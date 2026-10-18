"""Fill the empty GENRE tag of FLAC files already in the library.

For every FLAC listed in the library ISRC index whose GENRE tag is empty, ask the
genre fallback (Deezer public API by ISRC, then Spotify per-artist genres) and write
the result in the same format orpheus uses (joined with metadata_separator unless
split_metadata is on). Files that already have a genre are never touched.

    python genre_backfill.py --dir Z:\\ --dry --limit 200      # preview, writes nothing
    python genre_backfill.py --dir Z:\\                          # real run, resumable

Resumable: config/genre_backfill.db remembers every file already handled.
Files with no genre found anywhere go to config/genre_backfill_missing.csv.
"""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
CONFIG_DIR = os.path.join(SCRIPT_DIR, 'config')

from utils import genre_lookup  # noqa: E402


def load_settings():
    try:
        with open(os.path.join(CONFIG_DIR, 'settings.json'), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def index_path(root):
    """Same naming as IsrcLibraryIndex: one db per library root."""
    from orpheus.isrc_library_index import IsrcLibraryIndex
    idx = IsrcLibraryIndex(root, CONFIG_DIR, print_fn=lambda *a, **k: None)
    path = idx.db_path
    built = idx.is_built()
    idx.close()
    return path, built


def read_genre(path):
    from mutagen.flac import FLAC
    f = FLAC(path)
    vals = f.get('genre') or f.get('GENRE') or []
    return f, any(str(v).strip() for v in vals)


def write_genre(path, genres, separator, split):
    from mutagen.flac import FLAC
    f = FLAC(path)
    f['genre'] = list(genres) if split else [separator.join(genres)]
    f.save()


def main():
    for _stream in (sys.stdout, sys.stderr):     # file names with characters the console can't show
        try:
            _stream.reconfigure(errors='replace')
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dir', default=None, help='Library root (default: settings download_path).')
    ap.add_argument('--dry', action='store_true', help='Look up and report, write nothing.')
    ap.add_argument('--limit', type=int, default=0, help='Stop after N files that needed a lookup.')
    ap.add_argument('--workers', type=int, default=6, help='Parallel tag readers (NAS latency).')
    ap.add_argument('--spotify', action='store_true',
                    help='Also use Spotify when Deezer has nothing. OFF by default: the Spotify Developer Terms '
                    '(IV.3) limit storing its content, and the result is written into the files.')
    args = ap.parse_args()

    settings = load_settings()
    fmt = (settings.get('global') or {}).get('formatting') or {}
    separator = fmt.get('metadata_separator') or ', '
    split = bool(fmt.get('split_metadata'))
    root = args.dir or (settings.get('global', {}).get('general', {}).get('download_path'))
    if not root:
        raise SystemExit('No library root: pass --dir.')

    db_path, built = index_path(root)
    if not built:
        raise SystemExit(f'ISRC index for "{root}" is not built. Run:  python isrc_index_tool.py --dir "{root}" --build')

    spotify = None
    if args.spotify:
        try:
            import types
            from utils.spotify_isrc import SpotifyIsrcLookup
            cfg = (settings.get('modules') or {}).get('spotify') or {}
            if cfg.get('client_id') and cfg.get('client_secret'):
                spotify = SpotifyIsrcLookup(types.SimpleNamespace(config=cfg), CONFIG_DIR, print_fn=print)
        except Exception as e:
            print(f'Spotify disabled ({e})')
    lookup = genre_lookup.GenreLookup(spotify=spotify, use_spotify=spotify is not None)

    # progress db (resume)
    tag = hashlib.sha1(os.path.abspath(root).lower().encode('utf-8')).hexdigest()[:10]
    prog = sqlite3.connect(os.path.join(CONFIG_DIR, 'genre_backfill.db'), check_same_thread=False)
    prog.execute('CREATE TABLE IF NOT EXISTS done(root TEXT, path TEXT, status TEXT, PRIMARY KEY(root, path))')
    prog.commit()
    done = {r[0] for r in prog.execute('SELECT path FROM done WHERE root=?', (tag,))}

    src = sqlite3.connect(db_path)
    rows = [(p, i) for p, i in src.execute("SELECT path, isrc FROM files WHERE isrc != '' AND lower(path) LIKE '%.flac'")
            if p not in done]
    src.close()
    print(f'{len(rows)} FLAC file(s) to check ({len(done)} already handled in earlier runs). '
          f'{"DRY RUN - nothing is written. " if args.dry else ""}Spotify: {"on" if spotify else "off"}')

    lock = threading.Lock()
    stats = {'checked': 0, 'had': 0, 'filled': 0, 'missing': 0, 'errors': 0, 'lookups': 0}
    missing = []
    t0 = time.time()
    stop = threading.Event()

    def mark(path, status):
        if args.dry:                    # a preview must not change what a real run will do
            return
        with lock:
            prog.execute('INSERT OR REPLACE INTO done VALUES (?,?,?)', (tag, path, status))

    def work(item):
        path, isrc = item
        if stop.is_set():
            return
        try:
            _f, has = read_genre(path)
        except Exception:
            with lock:
                stats['errors'] += 1
            return                      # unreadable now -> retried next run
        with lock:
            stats['checked'] += 1
        if has:
            with lock:
                stats['had'] += 1
            mark(path, 'had')
            return
        with lock:
            if args.limit and stats['lookups'] >= args.limit:
                stop.set()
                return
            stats['lookups'] += 1
        genres = lookup.genres(isrc)
        if not genres:
            with lock:
                stats['missing'] += 1
                missing.append((path, isrc))
            mark(path, 'missing')
            return
        if args.dry:
            with lock:
                stats['filled'] += 1
                if stats['filled'] <= 15:
                    print(f'  would set {genres}  <- {os.path.basename(path)}')
            return
        try:
            write_genre(path, genres, separator, split)
        except Exception as e:
            with lock:
                stats['errors'] += 1
            print(f'  could not write {path}: {e}')
            return
        with lock:
            stats['filled'] += 1
        mark(path, 'filled')

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            for n, _ in enumerate(ex.map(work, rows), 1):
                if n % 500 == 0:
                    with lock:
                        prog.commit()
                    lookup.save()
                    print(f'  {n}/{len(rows)}  {stats}  {time.time() - t0:.0f}s', flush=True)
                if stop.is_set():
                    break
    except KeyboardInterrupt:
        stop.set()
        print('\n^C - stopping; progress is saved, run again to resume.')
    finally:
        prog.commit()
        lookup.save()

    if missing and not args.dry:
        out = os.path.join(CONFIG_DIR, 'genre_backfill_missing.csv')
        new = not os.path.exists(out)
        with open(out, 'a', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            if new:
                w.writerow(['path', 'isrc'])
            w.writerows(missing)
        print(f'No genre found for {len(missing)} file(s) -> {out}')
    print(f'Done. {stats}  ({time.time() - t0:.0f}s)')


if __name__ == '__main__':
    main()
