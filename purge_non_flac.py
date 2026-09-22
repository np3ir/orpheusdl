#!/usr/bin/env python3
"""Safely remove non-FLAC audio (m4a/mp3/...) from the library by MOVING it to a
reversible quarantine folder (never a hard delete). Works on network drives,
where the Windows Recycle Bin is not available.

    python purge_non_flac.py                 # DRY RUN: list what would be moved
    python purge_non_flac.py --apply         # move matches to <root>\\_no_flac_quarantine
    python purge_non_flac.py --dir "Z:\\" --ext m4a,mp3 --apply
    python purge_non_flac.py --rollback config/purge_non_flac_journal.csv   # undo

Defaults: --dir = download_path from config/settings.json, --ext = m4a,mp3,
quarantine = <root>\\_no_flac_quarantine (excluded from the ISRC index/dedup).
Moving within the same volume is instant (a rename, not a copy). Skips the dedupe
quarantine (_duplicados) and *.orpheus-* temp files. Writes a CSV of the targets
and an undo journal, and drops the moved files from the ISRC index.
When you're sure, delete the quarantine folder yourself to free space.
"""
import argparse
import csv
import json
import os
import shutil
import sys
import time

# Console may be cp1252; library paths contain non-Latin1 chars. Don't crash on print.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(SCRIPT_DIR, 'config')


def log(*a):
    print(*a, flush=True)


def default_dir():
    try:
        with open(os.path.join(CONFIG_DIR, 'settings.json'), encoding='utf-8') as f:
            dp = json.load(f)['global']['general']['download_path']
        return dp
    except Exception:
        return None


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}'
        n /= 1024


def collect(root, exts):
    exts = {('.' + e.lower().lstrip('.')) for e in exts}
    targets = []
    for dirpath, dirs, files in os.walk(root):
        # never touch the dedupe quarantine
        dirs[:] = [d for d in dirs if d.lower() != '_duplicados']
        for name in files:
            if name.startswith('.orpheus-'):
                continue
            if os.path.splitext(name)[1].lower() in exts:
                p = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                targets.append((p, size))
    return targets


QUARANTINE_NAME = '_no_flac_quarantine'  # excluded from the ISRC index/dedup


def _unique_dest(dest):
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    i = 2
    while os.path.exists(f'{base} ({i}){ext}'):
        i += 1
    return f'{base} ({i}){ext}'


def quarantine_move(paths, root, qdir, journal_path):
    """Move each path into qdir, preserving its path relative to root. Same volume
    => instant rename, no copy. Writes a journal (orig,dest) so it can be undone.
    Returns list of (orig, dest) actually moved. The Recycle Bin can't be used on
    a network drive, so this reversible move is the safe equivalent."""
    root = os.path.abspath(root)
    qdir = os.path.abspath(qdir)
    moved = []
    for p in paths:
        p = os.path.abspath(p)
        if not os.path.isfile(p):
            continue
        try:
            rel = os.path.relpath(p, root)
        except ValueError:
            rel = os.path.basename(p)
        dest = _unique_dest(os.path.join(qdir, rel))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(p, dest)
            moved.append((p, dest))
        except Exception as e:
            log('  no se pudo mover / could not move:', p.encode('ascii', 'backslashreplace').decode(), '::', e)
    # append to journal (so repeated runs accumulate an undo log)
    new = not os.path.exists(journal_path)
    with open(journal_path, 'a', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(['when', 'original_path', 'quarantine_path'])
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        for orig, dest in moved:
            w.writerow([stamp, orig, dest])
    return moved


def rollback(journal_path):
    """Move quarantined files back to their original paths using the journal."""
    if not os.path.isfile(journal_path):
        raise SystemExit(f'No journal found: {journal_path}')
    rows = list(csv.reader(open(journal_path, encoding='utf-8-sig')))[1:]
    back = fail = 0
    for row in rows:
        if len(row) < 3:
            continue
        _when, orig, dest = row[0], row[1], row[2]
        if os.path.isfile(orig):
            continue  # already back
        if not os.path.isfile(dest):
            continue  # nothing to restore
        try:
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            shutil.move(dest, orig)
            back += 1
        except Exception as e:
            fail += 1
            log('  no se pudo restaurar / could not restore:', orig.encode('ascii', 'backslashreplace').decode(), '::', e)
    log(f'Restaurados / restored: {back} | fallidos / failed: {fail}')
    return back


def main():
    ap = argparse.ArgumentParser(
        description='Move non-FLAC audio to a reversible quarantine folder (safe; works on network drives).')
    ap.add_argument('--dir', default=None, help='Library root (default: download_path from settings).')
    ap.add_argument('--ext', default='m4a,mp3', help='Comma list of extensions to move (default: m4a,mp3).')
    ap.add_argument('--apply', action='store_true', help='Actually move the files (default: dry run).')
    ap.add_argument('--quarantine', default=None,
                    help=f'Quarantine folder (default: <root>\\{QUARANTINE_NAME}).')
    ap.add_argument('--journal', default=None,
                    help='Undo log CSV (default: config/purge_non_flac_journal.csv).')
    ap.add_argument('--rollback', metavar='JOURNAL', default=None,
                    help='Undo a previous move: restore files listed in this journal, then exit.')
    ap.add_argument('--out', default=None, help='CSV report path (default: config/purge_non_flac.csv).')
    args = ap.parse_args()

    journal = args.journal or os.path.join(CONFIG_DIR, 'purge_non_flac_journal.csv')

    if args.rollback:
        log(f'Rollback desde / from: {args.rollback}')
        rollback(args.rollback)
        log('Recuerda reconstruir el índice: python isrc_index_tool.py --dir "<root>" --build')
        return

    root = args.dir or default_dir()
    if not root or not os.path.isdir(root):
        raise SystemExit(f'Library folder not found: {root!r}. Use --dir.')
    root = os.path.abspath(root)
    exts = [e for e in args.ext.split(',') if e.strip()]
    out = args.out or os.path.join(CONFIG_DIR, 'purge_non_flac.csv')
    qdir = os.path.abspath(args.quarantine or os.path.join(root, QUARANTINE_NAME))

    log(f'Scanning {root} for: {", ".join("."+e.lstrip(".") for e in exts)} ...')
    targets = collect(root, exts)
    total = sum(s for _p, s in targets)
    log(f'Found {len(targets)} file(s), {human(total)} total.')
    log(f'Quarantine folder: {qdir}')

    try:
        with open(out, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.writer(fh)
            w.writerow(['path', 'size_bytes'])
            for p, s in targets:
                w.writerow([p, s])
        log(f'List written: {out}')
    except Exception as e:
        log(f'(could not write CSV: {e})')

    if not targets:
        log('Nothing to do.')
        return
    for p, s in targets[:10]:
        log('  ' + p.encode('ascii', 'backslashreplace').decode() + f'  ({human(s)})')
    if len(targets) > 10:
        log(f'  ... and {len(targets) - 10} more (see the CSV)')

    if not args.apply:
        log('\nDRY RUN — nada movido. Re-run with --apply to move these to the quarantine folder.')
        return

    log(f'\nMoving {len(targets)} file(s) to quarantine ...')
    moved = quarantine_move([p for p, _s in targets], root, qdir, journal)
    log(f'Movidos / moved: {len(moved)}/{len(targets)}. Journal: {journal}')

    # keep the ISRC index consistent (drop the moved originals)
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from orpheus.isrc_library_index import IsrcLibraryIndex
        idx = IsrcLibraryIndex(root, CONFIG_DIR, print_fn=log)
        for orig, _dest in moved:
            try:
                idx.remove(orig)
            except Exception:
                pass
        idx.close()
        log('ISRC index updated (removed entries).')
    except Exception as e:
        log(f'(index not updated automatically: {e}; it self-corrects on the next --build)')

    log(f'\nHecho / Done. Los archivos están en:\n  {qdir}')
    log('Revísalos y, cuando estés seguro, BORRA esa carpeta desde el servidor/explorador.')
    log(f'Para deshacer / to undo:  python purge_non_flac.py --rollback "{journal}"')


if __name__ == '__main__':
    main()
