#!/usr/bin/env python3
"""Artist cross-service best-quality FLAC downloader (matched by ISRC).

Give it ONE artist URL from Tidal, Deezer or Qobuz, e.g.

    python artist_best_quality.py https://tidal.com/artist/10411
    python artist_best_quality.py https://www.deezer.com/en/artist/221 -q 16
    python artist_best_quality.py https://play.qobuz.com/artist/29674 --dry

and it will:

  1. Read that artist's whole discography from the service in the link.
  2. Find the SAME artist on the other two services by matching ISRCs
     (the ISRC identifies the exact recording, so no wrong-match guessing).
  3. Build the UNION of every recording across the three services (deduplicated
     by ISRC), so a track that only exists on one service is still captured.
  4. Skip anything you already own (checked against the library ISRC index).
  5. For every remaining recording, pick the service that offers the best FLAC
     (24-bit hi-res > 16-bit) -- or the quality you ask for -- and download it
     from there. If the requested quality is not available, it falls back to the
     best FLAC that IS available instead of skipping the track (unless --exact).

Quality (-q / config default_quality):
    best | max        -> highest FLAC anywhere (hi-res if it exists)   [download @ hifi]
    hires | 24 | hifi -> want 24-bit hi-res; fall back to 16 if none   [download @ hifi]
    lossless | 16 | cd-> 16-bit/44.1 FLAC                              [download @ lossless]

Permanent defaults live in config/settings.json > global > artist_best_quality
(default_quality, prefer_order, dedup_with_library, library_root, credited_albums).
The -q flag and the other flags override them for a single run.

Nothing here re-implements downloading or tagging: the actual download is handed
to orpheus.py (one call per service/quality batch), so every existing feature
(FLAC-only, dedup, covers, lyrics, tagging) still applies. Failures are retried
on the next-best service by ISRC.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(SCRIPT_DIR, 'config')
SETTINGS_PATH = os.path.join(CONFIG_DIR, 'settings.json')
ALL_SERVICES = ('qobuz', 'tidal', 'deezer')

# netloc substring -> service
NETLOC_MAP = {
    'tidal.com': 'tidal',
    'deezer.com': 'deezer',
    'qobuz.com': 'qobuz',
}

TARGET_ALIASES = {
    'best': 'best', 'max': 'best',
    'hires': 'hires', 'hi-res': 'hires', 'hifi': 'hires', '24': 'hires',
    'lossless': 'lossless', '16': 'lossless', 'cd': 'lossless',
}


# Services whose per-ISRC lookup identifies the equivalent artist reliably.
# NB: Qobuz's catalog/get?track_isrc endpoint can hang with no timeout, so the
# Qobuz path below uses catalog/search (search('track', isrc)) instead, which is
# fast and returns album.artist.id. A name-search fallback + ISRC-overlap check
# still guards every non-ISRC resolution.
ISRC_RESOLVE_SERVICES = ('qobuz', 'tidal', 'deezer')


def log(*a):
    print(*a, flush=True)


def call_timeout(fn, seconds, default=None, label=None):
    """Run fn() in a daemon thread and abandon it if it exceeds `seconds`.

    Some upstream API calls have no network timeout and can hang forever; this
    guarantees the tool keeps moving. A timed-out thread is left as a daemon
    (harmless; dies with the process) rather than blocking a shared pool."""
    box = {}

    def run():
        try:
            box['v'] = fn()
        except Exception as e:
            box['e'] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        if label:
            log(f'    (timeout {seconds}s: {label})')
        return default
    if 'e' in box:
        if label:
            log(f'    (error {label}: {box["e"]})')
        return default
    return box.get('v', default)


def track_url(service, tid):
    if service == 'qobuz':
        return f'https://open.qobuz.com/track/{tid}'
    if service == 'tidal':
        return f'https://tidal.com/browse/track/{tid}'
    if service == 'deezer':
        return f'https://www.deezer.com/track/{tid}'
    raise ValueError(service)


def tidal_quality(audio_quality):
    """Map a TIDAL audioQuality string to an (bit_depth, sample_rate) proxy.

    LOSSLESS is CD FLAC; HI_RES* is hi-res FLAC. HIGH/LOW are lossy (AAC), so
    they are not valid FLAC sources -> (0, 0) means 'not a FLAC candidate'.
    """
    q = (audio_quality or '').upper()
    if q in ('HI_RES_LOSSLESS', 'HI_RES'):
        return (24, 96.0)
    if q == 'LOSSLESS':
        return (16, 44.1)
    return (0, 0.0)


# --------------------------------------------------------------------------- #
# Settings / config
# --------------------------------------------------------------------------- #
def load_settings():
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def abq_config(settings):
    g = (settings.get('global') or {})
    cfg = dict(g.get('artist_best_quality') or {})
    cfg.setdefault('default_quality', 'best')
    cfg.setdefault('prefer_order', list(ALL_SERVICES))
    cfg.setdefault('dedup_with_library', True)
    cfg.setdefault('library_root', '')
    cfg.setdefault('credited_albums', False)
    # keep only known services, preserve order, append any missing
    order = [s for s in cfg['prefer_order'] if s in ALL_SERVICES]
    for s in ALL_SERVICES:
        if s not in order:
            order.append(s)
    cfg['prefer_order'] = order
    return cfg


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #
def parse_artist_url(url):
    """Return (service, artist_id) for a Tidal/Deezer/Qobuz artist URL."""
    u = urlparse(url if '://' in url else 'https://' + url)
    service = None
    for netloc_sub, svc in NETLOC_MAP.items():
        if netloc_sub in u.netloc:
            service = svc
            break
    if not service:
        raise SystemExit(f'Unrecognised service in URL netloc: "{u.netloc}"')
    parts = [p for p in u.path.split('/') if p]
    if 'artist' not in parts:
        raise SystemExit(f'Not an artist URL (no /artist/ segment): {url}')
    # id is the segment right after 'artist', else the last numeric segment
    idx = parts.index('artist')
    artist_id = None
    if idx + 1 < len(parts):
        artist_id = parts[idx + 1]
    if not (artist_id and artist_id.isdigit()):
        nums = [p for p in parts if p.isdigit()]
        artist_id = nums[-1] if nums else artist_id
    if not artist_id:
        raise SystemExit(f'Could not extract an artist id from: {url}')
    return service, str(artist_id)


# --------------------------------------------------------------------------- #
# Orpheus core / modules
# --------------------------------------------------------------------------- #
class Core:
    """Loads the Orpheus core and caches service modules (with raw .session)."""

    def __init__(self):
        self._example_moved = False
        ex = os.path.join(SCRIPT_DIR, 'modules', 'example')
        off = os.path.join(SCRIPT_DIR, 'modules', '_example_off')
        # self-heal: a previous run killed mid-way may have left it renamed
        if os.path.isdir(off) and not os.path.isdir(ex):
            try:
                os.rename(off, ex)
            except Exception:
                pass
        if os.path.isdir(ex):
            try:
                os.rename(ex, off)
                self._example_moved = True
            except Exception:
                pass
        from orpheus.core import Orpheus
        self.orpheus = Orpheus()
        self._modules = {}

    def module(self, name):
        if name not in self._modules:
            log(f'  loading module: {name} ...')
            self._modules[name] = (self.orpheus.load_module(name)
                                   or self.orpheus.loaded_modules.get(name))
        return self._modules[name]

    def session(self, name):
        return getattr(self.module(name), 'session', None)

    def close(self):
        if self._example_moved:
            ex = os.path.join(SCRIPT_DIR, 'modules', 'example')
            off = os.path.join(SCRIPT_DIR, 'modules', '_example_off')
            if os.path.isdir(off):
                try:
                    os.rename(off, ex)
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# Discography enumeration  ->  {isrc: (track_id, bit_depth, sample_rate)}
# --------------------------------------------------------------------------- #
def enumerate_qobuz(session, artist_id, credited, max_albums=0):
    out = {}
    artist = call_timeout(lambda: session.get_artist(str(artist_id)), 60,
                          default=None, label='qobuz get_artist')
    if not artist:
        log('  qobuz: get_artist failed/empty')
        return out
    albums = (artist.get('albums') or {}).get('items') or []
    if max_albums:
        albums = albums[:max_albums]
    log(f'  qobuz: {len(albums)} albums')
    for alb in albums:
        aid = alb.get('id')
        if aid is None:
            continue
        data = call_timeout(lambda: session.get_album(str(aid)), 45,
                            default=None, label=f'qobuz album {aid}')
        if not data:
            continue
        bd = data.get('maximum_bit_depth') or 16
        sr = data.get('maximum_sampling_rate') or 44.1
        if not data.get('hires_streamable', False):
            bd, sr = min(bd, 16), min(sr, 44.1)
        for tr in (data.get('tracks') or {}).get('items') or []:
            isrc = (tr.get('isrc') or '').strip().upper()
            tid = tr.get('id')
            if isrc and tid is not None:
                _keep_best(out, isrc, (str(tid), int(bd), float(sr)))
        time.sleep(0.1)
    return out


def enumerate_tidal(session, artist_id, credited, max_albums=0):
    out = {}
    album_ids = []
    for fn in ('get_artist_albums', 'get_artist_albums_ep_singles'):
        res = call_timeout(lambda fn=fn: getattr(session, fn)(str(artist_id)), 45,
                           default=None, label=f'tidal {fn}') or {}
        album_ids += [it.get('id') for it in (res.get('items') or []) if it.get('id') is not None]
    album_ids = list(dict.fromkeys(album_ids))
    if max_albums:
        album_ids = album_ids[:max_albums]
    log(f'  tidal: {len(album_ids)} albums/EPs')
    for aid in album_ids:
        res = call_timeout(lambda: session.get_album_items_all(str(aid)), 60,
                           default=None, label=f'tidal album {aid}')
        if not res:
            continue
        for row in res.get('items') or []:
            item = row.get('item') if isinstance(row, dict) and 'item' in row else row
            if not isinstance(item, dict):
                continue
            isrc = (item.get('isrc') or '').strip().upper()
            tid = item.get('id')
            bd, sr = tidal_quality(item.get('audioQuality'))
            if isrc and tid is not None and bd:  # bd==0 -> lossy, not a FLAC source
                _keep_best(out, isrc, (str(tid), bd, sr))
        time.sleep(0.1)
    return out


def enumerate_deezer(session, artist_id, credited, max_albums=0):
    out = {}
    album_ids, start, page = [], 0, 200
    while True:
        batch = call_timeout(
            lambda start=start: session.get_artist_album_ids(str(artist_id), start, page, credited),
            45, default=None, label='deezer discography')
        if not batch:
            break
        album_ids += [str(a) for a in batch]
        if len(batch) < page:
            break
        start += page
    album_ids = list(dict.fromkeys(album_ids))
    if max_albums:
        album_ids = album_ids[:max_albums]
    log(f'  deezer: {len(album_ids)} albums')
    missing_isrc = 0
    for aid in album_ids:
        album = call_timeout(lambda: session.get_album(str(aid)), 30,
                             default=None, label=f'deezer album {aid}')
        if not album:
            continue
        for tr in (album.get('SONGS') or {}).get('data') or []:
            isrc = (tr.get('ISRC') or '').strip().upper()
            tid = tr.get('SNG_ID')
            if tid is None:
                continue
            if not isrc:
                missing_isrc += 1
                continue
            _keep_best(out, isrc, (str(tid), 16, 44.1))  # Deezer FLAC = 16/44.1
        time.sleep(0.1)
    if missing_isrc:
        log(f'  deezer: {missing_isrc} tracks had no ISRC in album data (skipped for discovery)')
    return out


ENUMERATORS = {'qobuz': enumerate_qobuz, 'tidal': enumerate_tidal, 'deezer': enumerate_deezer}


def artist_name_of(core, service, artist_id):
    """Cheap artist-name lookup (one request), avoiding the heavy get_artist_info
    metadata batch that would duplicate our own enumeration."""
    try:
        s = core.session(service)
        if service == 'deezer':
            return s.get_artist_name(str(artist_id))
        if service == 'tidal':
            return (s.get_artist(str(artist_id)) or {}).get('name')
        if service == 'qobuz':
            a = s.get_artist(str(artist_id)) or {}
            return a.get('name') or (a.get('artist') or {}).get('name')
    except Exception:
        return None
    return None


def _keep_best(mapping, isrc, cand):
    """Keep the highest (bit_depth, sample_rate) candidate seen for an isrc within
    a single service's discography (some tracks appear on many editions)."""
    old = mapping.get(isrc)
    if old is None or (cand[1], cand[2]) > (old[1], old[2]):
        mapping[isrc] = cand


# --------------------------------------------------------------------------- #
# Equivalent-artist resolution on the other services (by ISRC, name fallback)
# --------------------------------------------------------------------------- #
def resolve_artist(core, service, sample_isrcs, artist_name):
    """Return (artist_id, method) where method is 'isrc' (trusted) or 'name'
    (needs an ISRC-overlap check afterwards)."""
    if service in ISRC_RESOLVE_SERVICES:
        votes = {}
        for isrc in sample_isrcs:
            aid = _artist_id_by_isrc(core, service, isrc)
            if aid:
                votes[aid] = votes.get(aid, 0) + 1
            if sum(votes.values()) >= 6:
                break
        if votes:
            best = max(votes, key=votes.get)
            log(f'  {service}: artist id {best} (ISRC match, {votes[best]} votes)')
            return best, 'isrc'
    # name search via the module interface (also the fallback for ISRC services)
    if artist_name:
        try:
            from utils.models import DownloadTypeEnum
            res = call_timeout(
                lambda: core.module(service).search(DownloadTypeEnum.artist, artist_name, limit=5),
                40, default=None, label=f'{service} artist name search')
            for item in res or []:
                if item.result_id:
                    log(f'  {service}: artist id {item.result_id} (name search "{item.name}")')
                    return str(item.result_id), 'name'
        except Exception as e:
            log(f'  {service}: name search failed: {e}')
    log(f'  {service}: could not resolve the artist (skipped)')
    return None, None


def _artist_id_by_isrc(core, service, isrc):
    if service == 'deezer':
        try:
            r = requests.get(f'https://api.deezer.com/track/isrc:{isrc}', timeout=20).json()
            return str(((r.get('artist') or {}).get('id'))) if r.get('artist') else None
        except Exception:
            return None
    if service == 'tidal':
        res = call_timeout(lambda: core.session('tidal').get_tracks_by_isrc(isrc),
                           15, default=None, label=f'tidal isrc {isrc}')
        items = res.get('items') if isinstance(res, dict) else res
        if items:
            it = items[0]
            arts = it.get('artists') or ([it.get('artist')] if it.get('artist') else [])
            if arts and arts[0]:
                return str(arts[0].get('id'))
    if service == 'qobuz':
        # catalog/search by ISRC (the catalog/get track_isrc endpoint can hang)
        res = call_timeout(lambda: core.session('qobuz').search('track', isrc, limit=3),
                           15, default=None, label=f'qobuz isrc {isrc}')
        tracks = (res.get('tracks') or {}).get('items') if isinstance(res, dict) else None
        for tr in tracks or []:
            if (tr.get('isrc') or '').strip().upper() != isrc.strip().upper():
                continue
            a = (tr.get('album') or {}).get('artist') or tr.get('performer') or {}
            if a.get('id') is not None:
                return str(a.get('id'))
    return None


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def choose_service(cands, target, prefer):
    """cands: {service: (id, bd, sr)}. Return an ORDERED list of services to try
    (best first) for this ISRC given the target quality."""
    present = [s for s in prefer if s in cands]
    if not present:
        return []
    if target == 'lossless':
        # 16-bit is the baseline everywhere a FLAC exists -> just preference order
        return present
    # best / hires -> rank by (bd, sr), tie-break by preference order
    return sorted(present,
                  key=lambda s: (cands[s][1], cands[s][2], -prefer.index(s)),
                  reverse=True)


# --------------------------------------------------------------------------- #
# Download (delegated to orpheus.py) + ISRC-based cross-service fallback
# --------------------------------------------------------------------------- #
def run_orpheus(urls, dlq, out_path):
    cmd = [sys.executable, 'orpheus.py', *urls, '-q', dlq]
    if out_path:
        cmd += ['-o', out_path]
    p = subprocess.run(cmd, cwd=SCRIPT_DIR, stdin=subprocess.DEVNULL)
    return p.returncode


def failed_isrcs_since(base, since):
    """Collect ISRCs written to error.txt files touched during this run."""
    found = set()
    base = str(base).rstrip('/\\')
    if not os.path.isdir(base):
        return found
    for dirpath, _dirs, files in os.walk(base):
        if 'error.txt' not in files:
            continue
        err = os.path.join(dirpath, 'error.txt')
        try:
            if os.path.getmtime(err) < since - 5:
                continue
            txt = open(err, encoding='utf-8', errors='ignore').read()
        except Exception:
            continue
        for m in re.finditer(r'\[ISRC:([A-Za-z0-9]+)\]', txt):
            found.add(m.group(1).strip().upper())
    return found


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    # Batch tool: never let an in-process module prompt (input()) block us.
    # Modules should already be logged in; detach stdin so a stray prompt fails
    # fast instead of hanging forever.
    try:
        sys.stdin = open(os.devnull)
    except Exception:
        pass

    settings = load_settings()
    cfg = abq_config(settings)

    ap = argparse.ArgumentParser(
        description='Download an artist as best-quality FLAC across Tidal/Deezer/Qobuz (matched by ISRC).')
    ap.add_argument('url', help='Artist URL (tidal.com/artist/.., deezer.com/.../artist/.., play.qobuz.com/artist/..)')
    ap.add_argument('-q', '--quality', default=None,
                    help='best|max | hires|24|hifi | lossless|16  (default from settings.json: %(default)s)')
    ap.add_argument('--exact', action='store_true',
                    help='With hires/24: skip a track if no hi-res exists instead of falling back to 16-bit.')
    ap.add_argument('--services', default=None,
                    help='Comma list / order of services to use, e.g. "qobuz,tidal". Default: config prefer_order.')
    ap.add_argument('--credited', dest='credited', action='store_true', default=None,
                    help='Include albums the artist only appears on (credited/appears-on).')
    ap.add_argument('--no-credited', dest='credited', action='store_false',
                    help='Only the artist main discography (default).')
    ap.add_argument('--no-dedup', action='store_true', help='Do not skip tracks already in the library ISRC index.')
    ap.add_argument('-o', '--output', default=None, help='Download output path (default: settings download_path).')
    ap.add_argument('--limit', type=int, default=0, help='Only process the first N recordings (for testing).')
    ap.add_argument('--max-albums', type=int, default=0,
                    help='Only read the first N albums per service (sampling/testing; 0 = all).')
    ap.add_argument('--dry', action='store_true', help='Show the plan (service + quality per track); download nothing.')
    args = ap.parse_args()

    target_raw = (args.quality or cfg['default_quality'] or 'best').strip().lower()
    target = TARGET_ALIASES.get(target_raw)
    if not target:
        raise SystemExit(f'Invalid quality "{args.quality}". Use: best | hires/24 | lossless/16')
    dlq = 'lossless' if target == 'lossless' else 'hifi'

    prefer = cfg['prefer_order']
    if args.services:
        chosen = [s.strip().lower() for s in args.services.split(',') if s.strip()]
        bad = [s for s in chosen if s not in ALL_SERVICES]
        if bad:
            raise SystemExit(f'Unknown service(s): {", ".join(bad)}. Choose from {", ".join(ALL_SERVICES)}')
        prefer = chosen + [s for s in prefer if s not in chosen]
        active = chosen
    else:
        active = list(prefer)

    credited = cfg['credited_albums'] if args.credited is None else args.credited
    out_path = args.output or (settings.get('global', {}).get('general', {}).get('download_path'))
    if out_path:
        out_path = out_path.rstrip('/\\') or out_path

    src_service, src_artist = parse_artist_url(args.url)
    if src_service not in active:
        active = [src_service] + active
        prefer = [src_service] + [s for s in prefer if s != src_service]
    log(f'Source: {src_service} artist {src_artist}')
    log(f'Target quality: {target} (download @ {dlq}); services: {", ".join(active)}; '
        f'credited={credited}; dedup={not args.no_dedup and cfg["dedup_with_library"]}')

    os.chdir(SCRIPT_DIR)
    core = Core()
    try:
        # ---- artist name (cheap; for logging + name-search fallback) ----
        artist_name = artist_name_of(core, src_service, src_artist)
        if artist_name:
            log(f'Artist: {artist_name}')

        # ---- enumerate the source discography ----
        log(f'\nEnumerating {src_service} discography...')
        per_service = {src_service: ENUMERATORS[src_service](
            core.session(src_service), src_artist, credited, args.max_albums)}
        log(f'  {src_service}: {len(per_service[src_service])} recordings with ISRC')

        source_isrcs = set(per_service[src_service].keys())
        sample_isrcs = list(source_isrcs)[:12]

        # ---- resolve + enumerate the other services ----
        for svc in active:
            if svc == src_service:
                continue
            log(f'\nResolving artist on {svc}...')
            aid, method = resolve_artist(core, svc, sample_isrcs, artist_name)
            if not aid:
                continue
            log(f'Enumerating {svc} discography...')
            mp = ENUMERATORS[svc](core.session(svc), aid, credited, args.max_albums)
            # Verify a name-resolved artist really is the same person: its ISRCs
            # must overlap the source. Guards the union against a same-name match.
            if method == 'name' and source_isrcs and mp:
                overlap = len(source_isrcs & set(mp.keys()))
                if overlap == 0:
                    log(f'  {svc}: 0 ISRC overlap with source -> likely the wrong '
                        f'"{artist_name}"; discarding {len(mp)} tracks.')
                    continue
                log(f'  {svc}: {overlap} shared ISRC(s) with source (name match verified)')
            per_service[svc] = mp
            log(f'  {svc}: {len(mp)} recordings with ISRC')

        # ---- union by ISRC ----
        union = {}  # isrc -> {service: (id, bd, sr)}
        for svc, mp in per_service.items():
            for isrc, cand in mp.items():
                union.setdefault(isrc, {})[svc] = cand
        log(f'\nUnion: {len(union)} unique recordings across {", ".join(per_service)}')

        # ---- library dedup ----
        idx = None
        skipped_have = 0
        if not args.no_dedup and cfg['dedup_with_library']:
            root = cfg['library_root'] or out_path
            try:
                from orpheus.isrc_library_index import IsrcLibraryIndex
                idx = IsrcLibraryIndex(root, CONFIG_DIR, print_fn=log)
                if not idx.is_built():
                    log(f'  [dedup] ISRC index for "{root}" not built yet -> not skipping anything.')
                    log(f'          Build it once with:  python isrc_index_tool.py --dir "{root}" --build')
                    idx = None
                else:
                    log(f'  [dedup] using ISRC index: {idx.db_path}')
            except Exception as e:
                log(f'  [dedup] disabled ({e})')
                idx = None

        # ---- plan ----
        plan = []  # (isrc, ordered_services)
        no_source = 0
        for isrc, cands in union.items():
            order = choose_service(cands, target, prefer)
            if not order:
                no_source += 1
                continue
            if args.exact and target == 'hires' and cands[order[0]][1] < 24:
                continue  # exact hi-res requested, none available
            if idx is not None:
                try:
                    if idx.find(isrc):
                        skipped_have += 1
                        continue
                except Exception:
                    pass
            plan.append((isrc, order))

        if idx is not None:
            idx.close()

        if args.limit and len(plan) > args.limit:
            plan = plan[:args.limit]

        log(f'\nTo download: {len(plan)}   |   already in library: {skipped_have}   |   '
            f'no FLAC source: {no_source}')

        # winners by primary service/quality
        primary_counts = {}
        for _isrc, order in plan:
            primary_counts[order[0]] = primary_counts.get(order[0], 0) + 1
        if primary_counts:
            log('  best-source split: ' + ', '.join(f'{s}={n}' for s, n in primary_counts.items()))

        if args.dry:
            log('\n=== DRY RUN (no downloads) ===')
            for isrc, order in plan[:200]:
                svc = order[0]
                tid, bd, sr = union[isrc][svc]
                log(f'  {isrc}  -> {svc} ({bd}bit/{sr}kHz)  {track_url(svc, tid)}')
            if len(plan) > 200:
                log(f'  ... and {len(plan) - 200} more')
            return

        if not plan:
            log('Nothing to download.')
            return

        # ---- download: one orpheus.py call per (primary service, dlq) batch ----
        run_start = time.time()
        by_service = {}
        for isrc, order in plan:
            svc = order[0]
            tid = union[isrc][svc][0]
            by_service.setdefault(svc, []).append(track_url(svc, tid))
        for svc in [s for s in prefer if s in by_service]:
            urls = by_service[svc]
            log(f'\n=== Downloading {len(urls)} track(s) from {svc} @ {dlq} ===')
            for i in range(0, len(urls), 40):
                run_orpheus(urls[i:i + 40], dlq, out_path)

        # ---- cross-service fallback for failures (by ISRC, next-best service) ----
        failed = failed_isrcs_since(out_path, run_start)
        retry = []
        for isrc, order in plan:
            if isrc in failed and len(order) > 1:
                retry.append((isrc, order[1:]))
        if retry:
            log(f'\n=== ISRC fallback: {len(retry)} track(s) failed on best source; '
                f'retrying on next-best service ===')
            for isrc, order in retry:
                for svc in order:
                    tid = union[isrc][svc][0]
                    log(f'  retry {isrc} via {svc}')
                    if run_orpheus([track_url(svc, tid)], dlq, out_path) == 0:
                        break

        log('\nDone.')
    finally:
        core.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('\n^C - aborted')
