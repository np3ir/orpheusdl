#!/usr/bin/env python
"""ISRC recovery for failed tracks.

After a download leaves failures in error.txt, re-download each failed track as
the *exact same recording* (matched by ISRC) from another service, trying
Deezer -> Tidal until one has it streamable. ISRC identifies the recording
uniquely, so this avoids the wrong-match problem of text search.

Works for any source (Qobuz, Spotify, ...): the ISRC is read straight from
error.txt when present (the core writes ``[ISRC:...]`` on each failure); only
tracks missing it fall back to a source-playlist lookup.

Usage (run from the OrpheusDL folder):
    python isrc_recover.py [path\\to\\error.txt]      # default: <download_path>\\error.txt
    python isrc_recover.py --dry                      # preview candidates, no downloads
"""
import os, re, sys, subprocess, requests


def _default_errfile():
    try:
        import json
        dp = json.load(open('config/settings.json', encoding='utf-8'))['global']['general']['download_path']
        dp = dp.rstrip('/\\') or dp
        return os.path.join(dp, 'error.txt')
    except Exception:
        return 'error.txt'


_paths = [a for a in sys.argv[1:] if not a.startswith('--')]
ERRFILE = _paths[0] if _paths else _default_errfile()
TRY_ORDER = ['deezer', 'tidal']          # services to try by ISRC, in order
ATTEMPT_TIMEOUT = 300                     # seconds per download attempt


def log(*a):
    print(*a, flush=True)


def parse_errfile(path):
    """Return (source_service, playlist_id, [ (id, isrc_or_None, 'Artist - Title'), ... ])."""
    t = open(path, encoding='utf-8', errors='ignore').read()
    svc = (re.search(r'#\s*Service:\s*(\w+)', t) or [None, None])[1]
    plm = re.search(r'#\s*Playlist:.*\((\d+)\)', t)
    entries, seen = [], set()
    # each failure line: "[pos] Artist - Title (id=XXX)[ | url][ [ISRC:YYYY]]"
    for m in re.finditer(r'\]\s*(.+?)\s*\(id=([^)\s]+)\)(?:[^\n]*?\[ISRC:([A-Za-z0-9]+)\])?', t):
        name, tid, isrc = m.group(1).strip(), m.group(2), m.group(3)
        if tid in seen:
            continue
        seen.add(tid)
        entries.append((tid, isrc, name))
    return (svc or '').lower(), (plm.group(1) if plm else None), entries


def resolve(entries, svc, pid):
    """Attach candidate {service: url} to each entry, loading modules only as needed."""
    need_source = any(not isrc for _, isrc, _ in entries) and svc and pid
    need_tidal = 'tidal' in TRY_ORDER
    tmods, id2 = {}, {}

    def isrc_url(service, isrc):
        try:
            if service == 'deezer':
                r = requests.get(f'https://api.deezer.com/track/isrc:{isrc}', timeout=20).json()
                return f"https://www.deezer.com/track/{r['id']}" if r.get('id') else None
            if service == 'tidal' and tmods.get('tidal'):
                res = tmods['tidal'].session.get_tracks_by_isrc(isrc)
                items = res.get('items') if isinstance(res, dict) else res
                if items:
                    it = items[0]
                    tid = it.get('id') if isinstance(it, dict) else getattr(it, 'id', None)
                    return f"https://tidal.com/browse/track/{tid}" if tid else None
        except Exception:
            return None
        return None

    out = []
    if not (need_source or need_tidal):
        for tid, isrc, name in entries:
            urls = [('deezer', isrc_url('deezer', isrc))] if isrc else []
            out.append({'id': tid, 'isrc': isrc, 'name': name,
                        'urls': [(s, u) for s, u in urls if u]})
        return out

    EX, OFF = 'modules/example', 'modules/_example_off'
    moved = os.path.isdir(EX)
    if moved:
        os.rename(EX, OFF)
    try:
        from orpheus.core import Orpheus
        o = Orpheus()
        if need_source:
            log("loading source module for missing ISRCs:", svc)
            src = o.load_module(svc) or o.loaded_modules.get(svc)
            for off in range(0, 20000, 500):
                pl = src.session.get_playlist(pid, limit=500, offset=off)
                items = (pl.get('tracks') or {}).get('items') or []
                for tr in items:
                    id2[str(tr.get('id'))] = tr.get('isrc')
                if not items:
                    break
        if need_tidal:
            log("loading tidal for ISRC lookup (slow, please wait)...")
            try:
                tmods['tidal'] = o.load_module('tidal') or o.loaded_modules.get('tidal')
            except Exception as e:
                log("  tidal load failed:", e)
        for tid, isrc, name in entries:
            isrc = isrc or id2.get(tid)
            urls = []
            if isrc:
                for s in TRY_ORDER:
                    u = isrc_url(s, isrc)
                    if u:
                        urls.append((s, u))
            out.append({'id': tid, 'isrc': isrc, 'name': name, 'urls': urls})
            log(f"  {name} [{isrc}] -> {[s for s, _ in urls] or 'no candidates'}")
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
    svc, pid, entries = parse_errfile(ERRFILE)
    log(f"source={svc} playlist={pid} failures={len(entries)} "
        f"(inline ISRC: {sum(1 for _, i, _ in entries if i)}/{len(entries)})")
    if not entries:
        log("No failed tracks parsed from error.txt"); return
    resolved = resolve(entries, svc, pid)
    if '--dry' in sys.argv:
        log("\n=== DRY RUN (no downloads) ===")
        for e in resolved:
            log(f"  {e['name']} [{e['isrc']}] -> {[f'{s}:{u}' for s, u in e['urls']] or 'NO CANDIDATES'}")
        return
    recovered, failed = [], []
    for e in resolved:
        done = False
        for s, u in e['urls']:
            log(f"downloading via {s}: {e['name']}")
            try:
                if download(u):
                    log(f"  OK via {s}"); recovered.append((s, e)); done = True; break
                log(f"  {s} not available, trying next")
            except Exception as ex:
                log(f"  {s} error: {ex}")
        if not done:
            failed.append(e)
    log(f"\n=== RECOVERED {len(recovered)}/{len(resolved)} ===")
    for e in failed:
        log(f"  STILL FAILED: {e['name']} (isrc {e['isrc']})")


if __name__ == '__main__':
    main()
