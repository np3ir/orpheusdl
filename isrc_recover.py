#!/usr/bin/env python
"""ISRC recovery for failed playlist tracks.

After a playlist download leaves failures in error.txt, re-download each failed
track as the *exact same recording* (matched by ISRC) from another service,
trying Deezer -> Tidal until one has it streamable. This avoids the wrong-match
problem of text search: the ISRC identifies the recording uniquely.

Usage (run from the OrpheusDL folder, right after a playlist download):
    python isrc_recover.py [path\\to\\error.txt]      # default: D:\\Music\\error.txt

Notes:
- Reads the source service + playlist id + failed track ids from error.txt.
- Loads the source module once to map failed ids -> ISRC via the playlist.
- Deezer ISRC lookup is the public API (no auth); Tidal uses the logged-in module.
- Temporarily disables modules/example (a broken stub) so Orpheus can load, then
  restores it.
"""
import os, re, sys, subprocess, requests


def _default_errfile():
    """Default error.txt = <download_path>/error.txt, read from config/settings.json."""
    try:
        import json
        dp = json.load(open('config/settings.json', encoding='utf-8'))['global']['general']['download_path']
        dp = dp.rstrip('/\\') or dp
        return os.path.join(dp, 'error.txt')
    except Exception:
        return 'error.txt'


# first non-flag argument is the error.txt path; otherwise derive it from settings
_paths = [a for a in sys.argv[1:] if not a.startswith('--')]
ERRFILE = _paths[0] if _paths else _default_errfile()
TRY_ORDER = ['deezer', 'tidal']          # services to try by ISRC, in order
ATTEMPT_TIMEOUT = 300                     # seconds per download attempt


def log(*a):
    print(*a, flush=True)


def parse_errfile(path):
    t = open(path, encoding='utf-8', errors='ignore').read()
    svc = (re.search(r'#\s*Service:\s*(\w+)', t) or [None, None])[1]
    plm = re.search(r'#\s*Playlist:.*\((\d+)\)', t)
    ids = list(dict.fromkeys(re.findall(r'id=(\d+)', t)))
    return (svc or '').lower(), (plm.group(1) if plm else None), ids


def resolve(src_service, playlist_id, fail_ids):
    """Return {id: {isrc,title,artist,urls:[(service,url)]}} using loaded modules."""
    EX, OFF = 'modules/example', 'modules/_example_off'
    moved = os.path.isdir(EX)
    if moved:
        os.rename(EX, OFF)
    out = {}
    try:
        from orpheus.core import Orpheus
        o = Orpheus()
        log("loading source module:", src_service)
        src = o.load_module(src_service) or o.loaded_modules.get(src_service)
        id2 = {}
        for off in range(0, 20000, 500):
            pl = src.session.get_playlist(playlist_id, limit=500, offset=off)
            items = (pl.get('tracks') or {}).get('items') or []
            for tr in items:
                id2[str(tr.get('id'))] = (tr.get('isrc'), tr.get('title'),
                                          (tr.get('performer') or {}).get('name'))
            if not items:
                break
        log("playlist tracks indexed:", len(id2))
        mods = {}
        if 'tidal' in TRY_ORDER:
            log("loading tidal for ISRC lookup (slow, please wait)...")
            try:
                mods['tidal'] = o.load_module('tidal') or o.loaded_modules.get('tidal')
            except Exception as e:
                log("  tidal load failed:", e); mods['tidal'] = None

        def isrc_url(service, isrc):
            try:
                if service == 'deezer':
                    r = requests.get(f'https://api.deezer.com/track/isrc:{isrc}', timeout=20).json()
                    return f"https://www.deezer.com/track/{r['id']}" if r.get('id') else None
                if service == 'tidal' and mods.get('tidal'):
                    res = mods['tidal'].session.get_tracks_by_isrc(isrc)
                    items = res.get('items') if isinstance(res, dict) else res
                    if items:
                        it = items[0]
                        tid = it.get('id') if isinstance(it, dict) else getattr(it, 'id', None)
                        return f"https://tidal.com/browse/track/{tid}" if tid else None
            except Exception:
                return None
            return None

        for i in fail_ids:
            isrc, title, artist = id2.get(i, (None, None, None))
            entry = {'isrc': isrc, 'title': title, 'artist': artist, 'urls': []}
            if isrc:
                for s in TRY_ORDER:
                    u = isrc_url(s, isrc)
                    if u:
                        entry['urls'].append((s, u))
            out[i] = entry
            log(f"  {i} {artist} - {title} [{isrc}] -> {[s for s, _ in entry['urls']] or 'no candidates'}")
    finally:
        if moved and os.path.isdir(OFF):
            os.rename(OFF, EX)
    return out


def download(url):
    p = subprocess.run([sys.executable, 'orpheus.py', url], capture_output=True,
                       text=True, timeout=ATTEMPT_TIMEOUT, stdin=subprocess.DEVNULL)
    out = (p.stdout or '') + (p.stderr or '')
    return ('Track completed' in out) or ('already exists' in out.lower())


def main():
    if not os.path.isfile(ERRFILE):
        log("No error file:", ERRFILE); return
    svc, pid, ids = parse_errfile(ERRFILE)
    log(f"source={svc} playlist={pid} failures={len(ids)}")
    if not (svc and pid and ids):
        log("Could not parse service/playlist/ids from error.txt"); return
    resolved = resolve(svc, pid, ids)
    if '--dry' in sys.argv:
        log("\n=== DRY RUN (no downloads) — candidates per failed track ===")
        for i, e in resolved.items():
            log(f"  {e['artist']} - {e['title']} [{e['isrc']}] -> {[f'{s}:{u}' for s, u in e['urls']] or 'NO CANDIDATES'}")
        return
    recovered, failed = [], []
    for i, e in resolved.items():
        done = False
        for s, u in e['urls']:
            log(f"downloading via {s}: {e['artist']} - {e['title']}")
            try:
                if download(u):
                    log(f"  OK via {s}"); recovered.append((s, e)); done = True; break
                else:
                    log(f"  {s} not available, trying next")
            except Exception as ex:
                log(f"  {s} error: {ex}")
        if not done:
            failed.append(e)
    log(f"\n=== RECOVERED {len(recovered)}/{len(resolved)} ===")
    for e in failed:
        log(f"  STILL FAILED: {e['artist']} - {e['title']} (isrc {e['isrc']})")


if __name__ == '__main__':
    main()
