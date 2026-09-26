#!/usr/bin/env python3
"""Cross-service best-quality FLAC downloader (matched by ISRC).

Give it ONE link from Tidal, Deezer or Qobuz -- artist, album, playlist or track:

    python artist_best_quality.py https://tidal.com/artist/10411
    python artist_best_quality.py https://www.deezer.com/album/1603029 -q 16
    python artist_best_quality.py https://tidal.com/browse/track/96594261 --dry

and it will:

  1. Collect the recordings' ISRCs from the link:
     - artist  -> the whole discography, plus the SAME artist on the other two
       services (matched by ISRC; the union captures service-exclusive tracks);
     - album/playlist/track -> the ISRCs in that album, playlist or track.
  2. Look each recording up on all three services by ISRC (the ISRC identifies the
     exact recording, so no wrong-match guessing).
  3. Skip anything you already own (checked against the library ISRC index).
  4. For every remaining recording, pick the service that offers the best FLAC
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
import unicodedata
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
    'spotify.com': 'spotify',  # metadata-only SOURCE (no FLAC); never a download target
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
    cfg.setdefault('own_albums_only', False)
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
MEDIA_SEGMENTS = ('artist', 'album', 'playlist', 'track')


def parse_media_url(url):
    """Return (service, media_type, media_id) for a Tidal/Deezer/Qobuz link.
    media_type is one of artist/album/playlist/track."""
    u = urlparse(url if '://' in url else 'https://' + url)
    service = None
    for netloc_sub, svc in NETLOC_MAP.items():
        if netloc_sub in u.netloc:
            service = svc
            break
    if not service:
        raise SystemExit(f'Unrecognised service in URL netloc: "{u.netloc}"')
    parts = [p for p in u.path.split('/') if p]
    seg = next((p for p in parts if p in MEDIA_SEGMENTS), None)
    if not seg:
        raise SystemExit(
            f'Unsupported link: {url}\n'
            f'Use an artist, album, playlist or track URL.')
    idx = parts.index(seg)
    mid = parts[idx + 1] if idx + 1 < len(parts) else None
    if not (mid and mid.isdigit()):
        nums = [p for p in parts if p.isdigit()]
        mid = nums[-1] if nums else mid
    if not mid:
        raise SystemExit(f'Could not extract a {seg} id from: {url}')
    return service, seg, str(mid)


# --------------------------------------------------------------------------- #
# Orpheus core / modules
# --------------------------------------------------------------------------- #
class Core:
    """Loads the Orpheus core and caches service modules (with raw .session)."""

    def __init__(self):
        # NB: Orpheus loads fine with modules/example present, and we only ever
        # load_module() the services we need, so we do NOT rename example aside
        # (doing so would race with a second abq/orpheus running concurrently).
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
        pass


# --------------------------------------------------------------------------- #
# Discography enumeration  ->  {isrc: (track_id, bit_depth, sample_rate)}
# --------------------------------------------------------------------------- #
def _norm(s):
    """Casefold + strip accents/punctuation for tolerant name comparison."""
    s = unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode('ascii')
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in s.lower()).split())


def _track_artist_ids(track, service):
    ids = set()
    if service == 'qobuz':
        p = track.get('performer') or {}
        if p.get('id') is not None:
            ids.add(str(p['id']))
        for a in (track.get('artists') or []):
            if isinstance(a, dict) and a.get('id') is not None:
                ids.add(str(a['id']))
    elif service == 'tidal':
        arts = track.get('artists') or ([track.get('artist')] if track.get('artist') else [])
        for a in arts:
            if isinstance(a, dict) and a.get('id') is not None:
                ids.add(str(a['id']))
    elif service == 'deezer':
        if track.get('ART_ID') is not None:
            ids.add(str(track['ART_ID']))
        for a in (track.get('ARTISTS') or []):
            if isinstance(a, dict) and a.get('ART_ID') is not None:
                ids.add(str(a['ART_ID']))
    return ids


def _track_artist_names(track, service):
    names = set()
    if service == 'qobuz':
        p = track.get('performer') or {}
        if p.get('name'):
            names.add(p['name'])
        for a in (track.get('artists') or []):
            if isinstance(a, dict) and a.get('name'):
                names.add(a['name'])
        perf = track.get('performers')
        if isinstance(perf, str):
            for chunk in perf.split(' - '):
                nm = chunk.split(',')[0].strip()
                if nm:
                    names.add(nm)
    elif service == 'tidal':
        arts = track.get('artists') or ([track.get('artist')] if track.get('artist') else [])
        for a in arts:
            if isinstance(a, dict) and a.get('name'):
                names.add(a['name'])
    elif service == 'deezer':
        if track.get('ART_NAME'):
            names.add(track['ART_NAME'])
        for a in (track.get('ARTISTS') or []):
            if isinstance(a, dict) and a.get('ART_NAME'):
                names.add(a['ART_NAME'])
    return {_norm(n) for n in names if n}


def _name_matches(target_norm, name_norm):
    """Tolerant artist-name match. Handles alias entities like 'Nat King Cole' vs
    'Nat King Cole Trio' / 'The King Cole Trio' (shared name tokens) while still
    rejecting a genuinely different artist (e.g. 'Lester Young')."""
    if not target_norm or not name_norm:
        return False
    if target_norm == name_norm:
        return True
    if target_norm in name_norm or name_norm in target_norm:
        return True
    tt = set(target_norm.split())
    nt = set(name_norm.split())
    if not tt:
        return False
    overlap = len(tt & nt)
    # keep when at least half of the target's name tokens appear in the credit
    return overlap >= 1 and overlap / len(tt) >= 0.5


def _artist_on_track(track, service, artist_id, artist_name=None):
    """True if the target artist actually performs on this track. Keeps the track
    when it matches by artist **id** or by a tolerant **name** match (robust to a
    service's variant artist ids/aliases, e.g. Deezer tagging 'Nat King Cole Trio'
    with a different ART_ID). Filters out other artists' tracks that ride along on
    compilations / Various-Artists albums."""
    if str(artist_id) in _track_artist_ids(track, service):
        return True
    names = _track_artist_names(track, service)
    if artist_name and names:
        tn = _norm(artist_name)
        if any(_name_matches(tn, n) for n in names):
            return True
    if not names and not _track_artist_ids(track, service):
        return True  # no artist data on the track -> don't over-filter
    return False


def _album_artist_ids(album_obj, service):
    """Artist id(s) credited to the whole album (not per track)."""
    ids = set()
    if not isinstance(album_obj, dict):
        return ids
    if service in ('qobuz', 'tidal'):
        a = album_obj.get('artist') or {}
        if a.get('id') is not None:
            ids.add(str(a['id']))
        for x in (album_obj.get('artists') or []):
            if isinstance(x, dict) and x.get('id') is not None:
                ids.add(str(x['id']))
    elif service == 'deezer':
        d = album_obj.get('DATA') if isinstance(album_obj.get('DATA'), dict) else album_obj
        if d.get('ART_ID') is not None:
            ids.add(str(d['ART_ID']))
        for x in (d.get('ARTISTS') or []):
            if isinstance(x, dict) and x.get('ART_ID') is not None:
                ids.add(str(x['ART_ID']))
    return ids


def fetch_album_retry(fn, seconds, label, attempts=3, wait=3.0):
    """call_timeout with retries: a transient timeout/hiccup on a slow API or NAS
    must not silently drop an album from the union (that loses a real FLAC source
    for the whole artist run). Returns the result, or None after all attempts."""
    for n in range(1, attempts + 1):
        res = call_timeout(fn, seconds, default=None, label=label)
        if res:
            return res
        if n < attempts:
            time.sleep(wait)
    return None


def _report_failed_albums(service, failed):
    if failed:
        shown = ', '.join(str(a) for a in failed[:20])
        more = f' … (+{len(failed) - 20})' if len(failed) > 20 else ''
        log(f'  ⚠️ {service}: {len(failed)} album(s) could NOT be read after retries '
            f'-> excluded from the union (may lose FLAC sources): {shown}{more}')


def enumerate_qobuz(session, artist_id, credited, max_albums=0, artist_filter=True,
                    own_albums_only=False, artist_name=None):
    out = {}
    skipped = 0
    skipped_albums = 0
    failed_albums = []
    unavailable = 0
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
        data = fetch_album_retry(lambda: session.get_album(str(aid)), 45, f'qobuz album {aid}')
        if not data:
            failed_albums.append(aid)
            continue
        if own_albums_only:
            aids = _album_artist_ids(data, 'qobuz')
            if aids and str(artist_id) not in aids:
                skipped_albums += 1
                continue
        bd = data.get('maximum_bit_depth') or 16
        sr = data.get('maximum_sampling_rate') or 44.1
        if not data.get('hires_streamable', False):
            bd, sr = min(bd, 16), min(sr, 44.1)
        for tr in (data.get('tracks') or {}).get('items') or []:
            isrc = (tr.get('isrc') or '').strip().upper()
            tid = tr.get('id')
            if not (isrc and tid is not None):
                continue
            if tr.get('streamable') is False:  # pre-release/embargoed (streamable_at future) or pulled
                unavailable += 1
                continue
            if artist_filter and not _artist_on_track(tr, 'qobuz', artist_id, artist_name):
                skipped += 1
                continue
            _keep_best(out, isrc, (str(tid), int(bd), float(sr)))
        time.sleep(0.1)
    if skipped_albums:
        log(f'  qobuz: skipped {skipped_albums} album(s) not credited to this artist')
    if skipped:
        log(f'  qobuz: skipped {skipped} track(s) not performed by this artist')
    if unavailable:
        log(f'  qobuz: skipped {unavailable} track(s) not yet streamable (pre-release/unavailable)')
    _report_failed_albums('qobuz', failed_albums)
    return out


def enumerate_tidal(session, artist_id, credited, max_albums=0, artist_filter=True,
                    own_albums_only=False, artist_name=None):
    out = {}
    skipped = 0
    skipped_albums = 0
    failed_albums = []
    unavailable = 0
    album_ids = []
    alb_artist_ids = {}  # album id -> set of album-artist ids (from the listing)
    for fn in ('get_artist_albums', 'get_artist_albums_ep_singles'):
        res = call_timeout(lambda fn=fn: getattr(session, fn)(str(artist_id)), 45,
                           default=None, label=f'tidal {fn}') or {}
        for it in (res.get('items') or []):
            if it.get('id') is not None:
                album_ids.append(it.get('id'))
                alb_artist_ids[str(it.get('id'))] = _album_artist_ids(it, 'tidal')
    album_ids = list(dict.fromkeys(album_ids))
    if max_albums:
        album_ids = album_ids[:max_albums]
    log(f'  tidal: {len(album_ids)} albums/EPs')
    for aid in album_ids:
        if own_albums_only:
            aids = alb_artist_ids.get(str(aid)) or set()
            if aids and str(artist_id) not in aids:
                skipped_albums += 1
                continue
        res = fetch_album_retry(lambda: session.get_album_items_all(str(aid)), 60, f'tidal album {aid}')
        if not res:
            failed_albums.append(aid)
            continue
        for row in res.get('items') or []:
            item = row.get('item') if isinstance(row, dict) and 'item' in row else row
            if not isinstance(item, dict):
                continue
            isrc = (item.get('isrc') or '').strip().upper()
            tid = item.get('id')
            bd, sr = tidal_quality(item.get('audioQuality'))
            if not (isrc and tid is not None and bd):  # bd==0 -> lossy, not a FLAC source
                continue
            if item.get('streamReady') is False:  # pre-release (streamStartDate future) or pulled
                unavailable += 1
                continue
            if artist_filter and not _artist_on_track(item, 'tidal', artist_id, artist_name):
                skipped += 1
                continue
            _keep_best(out, isrc, (str(tid), bd, sr))
        time.sleep(0.1)
    if skipped_albums:
        log(f'  tidal: skipped {skipped_albums} album(s) not credited to this artist')
    if skipped:
        log(f'  tidal: skipped {skipped} track(s) not performed by this artist')
    if unavailable:
        log(f'  tidal: skipped {unavailable} track(s) not yet streamable (pre-release/unavailable)')
    _report_failed_albums('tidal', failed_albums)
    return out


def enumerate_deezer(session, artist_id, credited, max_albums=0, artist_filter=True,
                     own_albums_only=False, artist_name=None):
    out = {}
    skipped = 0
    skipped_albums = 0
    failed_albums = []
    unavailable = 0
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
        album = fetch_album_retry(lambda: session.get_album(str(aid)), 30, f'deezer album {aid}')
        if not album:
            failed_albums.append(aid)
            continue
        if own_albums_only:
            aids = _album_artist_ids(album, 'deezer')
            if aids and str(artist_id) not in aids:
                skipped_albums += 1
                continue
        for tr in (album.get('SONGS') or {}).get('data') or []:
            isrc = (tr.get('ISRC') or '').strip().upper()
            tid = tr.get('SNG_ID')
            if tid is None:
                continue
            if not isrc:
                missing_isrc += 1
                continue
            if (tr.get('RIGHTS') or {}).get('STREAM_SUB_AVAILABLE') is False:  # not streamable on sub (pre-release/pulled)
                unavailable += 1
                continue
            if artist_filter and not _artist_on_track(tr, 'deezer', artist_id, artist_name):
                skipped += 1
                continue
            _keep_best(out, isrc, (str(tid), 16, 44.1))  # Deezer FLAC = 16/44.1
        time.sleep(0.1)
    if skipped_albums:
        log(f'  deezer: skipped {skipped_albums} album(s) not credited to this artist')
    if missing_isrc:
        log(f'  deezer: {missing_isrc} tracks had no ISRC in album data (skipped for discovery)')
    if skipped:
        log(f'  deezer: skipped {skipped} track(s) not performed by this artist')
    if unavailable:
        log(f'  deezer: skipped {unavailable} track(s) not yet streamable (pre-release/unavailable)')
    _report_failed_albums('deezer', failed_albums)
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
# Per-ISRC probe: does `service` have this recording, and at what FLAC quality?
# --------------------------------------------------------------------------- #
def probe_isrc(core, service, isrc):
    """Return (track_id, bit_depth, sample_rate) if `service` has this ISRC as a
    FLAC candidate, else None. Used for track/album/playlist links, where there
    is no artist to enumerate -- each recording is looked up directly by ISRC."""
    up = isrc.strip().upper()
    if service == 'tidal':
        res = call_timeout(lambda: core.session('tidal').get_tracks_by_isrc(isrc),
                           15, default=None, label=f'tidal isrc {isrc}')
        items = res.get('items') if isinstance(res, dict) else res
        for it in items or []:
            bd, sr = tidal_quality(it.get('audioQuality'))
            if it.get('id') is not None and bd:
                return (str(it['id']), bd, sr)
        return None
    if service == 'deezer':
        try:
            r = requests.get(f'https://api.deezer.com/track/isrc:{isrc}', timeout=20).json()
            if r.get('id'):
                return (str(r['id']), 16, 44.1)  # Deezer FLAC = 16/44.1
        except Exception:
            return None
        return None
    if service == 'qobuz':
        res = call_timeout(lambda: core.session('qobuz').search('track', isrc, limit=3),
                           15, default=None, label=f'qobuz isrc {isrc}')
        tracks = (res.get('tracks') or {}).get('items') if isinstance(res, dict) else None
        for tr in tracks or []:
            if (tr.get('isrc') or '').strip().upper() != up:
                continue
            alb = tr.get('album') or {}
            bd = tr.get('maximum_bit_depth') or alb.get('maximum_bit_depth') or 16
            sr = tr.get('maximum_sampling_rate') or alb.get('maximum_sampling_rate') or 44.1
            if tr.get('id') is not None:
                return (str(tr['id']), int(bd), float(sr))
        return None
    return None


# --------------------------------------------------------------------------- #
# Source ISRC extraction for track / album / playlist links (no artist needed)
# --------------------------------------------------------------------------- #
def _u(x):
    return (x or '').strip().upper()


def source_isrcs(core, service, mtype, mid):
    """Return the set of ISRCs contained in a track/album/playlist/artist link."""
    if service == 'spotify':
        return _spotify_isrcs(core, mtype, mid)
    s = core.session(service)
    out = set()
    if mtype == 'track':
        if service == 'deezer':
            try:
                r = requests.get(f'https://api.deezer.com/track/{mid}', timeout=20).json()
                if _u(r.get('isrc')):
                    out.add(_u(r.get('isrc')))
            except Exception:
                pass
        else:  # tidal / qobuz: track object carries isrc
            t = call_timeout(lambda: s.get_track(str(mid)), 20, default=None,
                             label=f'{service} track {mid}') or {}
            if _u(t.get('isrc')):
                out.add(_u(t.get('isrc')))
    elif mtype == 'album':
        out |= _album_isrcs(core, service, mid)
    elif mtype == 'playlist':
        out |= _playlist_isrcs(core, service, mid)
    return out


def _album_isrcs(core, service, album_id):
    s = core.session(service)
    out = set()
    if service == 'qobuz':
        d = call_timeout(lambda: s.get_album(str(album_id)), 45, default=None,
                         label=f'qobuz album {album_id}') or {}
        for tr in (d.get('tracks') or {}).get('items') or []:
            if _u(tr.get('isrc')):
                out.add(_u(tr.get('isrc')))
    elif service == 'tidal':
        r = call_timeout(lambda: s.get_album_items_all(str(album_id)), 60, default=None,
                         label=f'tidal album {album_id}') or {}
        for row in r.get('items') or []:
            it = row.get('item') if isinstance(row, dict) and 'item' in row else row
            if isinstance(it, dict) and _u(it.get('isrc')):
                out.add(_u(it.get('isrc')))
    elif service == 'deezer':
        d = call_timeout(lambda: s.get_album(str(album_id)), 30, default=None,
                         label=f'deezer album {album_id}') or {}
        for tr in (d.get('SONGS') or {}).get('data') or []:
            if _u(tr.get('ISRC')):
                out.add(_u(tr.get('ISRC')))
    return out


def _playlist_isrcs(core, service, playlist_id):
    s = core.session(service)
    out = set()
    if service == 'qobuz':
        off = 0
        while True:
            d = call_timeout(lambda off=off: s.get_playlist(str(playlist_id), limit=500, offset=off),
                             45, default=None, label=f'qobuz playlist {playlist_id}') or {}
            items = (d.get('tracks') or {}).get('items') or []
            for tr in items:
                if _u(tr.get('isrc')):
                    out.add(_u(tr.get('isrc')))
            if len(items) < 500:
                break
            off += len(items)
    elif service == 'tidal':
        r = call_timeout(lambda: s.get_playlist_items(str(playlist_id)), 90, default=None,
                         label=f'tidal playlist {playlist_id}') or {}
        for row in r.get('items') or []:
            it = row.get('item') if isinstance(row, dict) and 'item' in row else row
            if isinstance(it, dict) and _u(it.get('isrc')):
                out.add(_u(it.get('isrc')))
    elif service == 'deezer':
        start = 0
        while True:
            d = call_timeout(lambda start=start: s.get_playlist(str(playlist_id), 500, start),
                             45, default=None, label=f'deezer playlist {playlist_id}') or {}
            items = (d.get('SONGS') or {}).get('data') or []
            for tr in items:
                if _u(tr.get('ISRC')):
                    out.add(_u(tr.get('ISRC')))
            if len(items) < 500:
                break
            start += len(items)
    return out


def _spotify_isrcs(core, mtype, mid):
    """ISRCs from a Spotify link. Spotify is metadata-only here (never a FLAC
    source): we read the ISRCs and download the exact recordings from the other
    services. Uses app-level / anonymous metadata, not an account login."""
    out = set()
    try:
        mod = core.module('spotify')
    except Exception as e:
        log(f'  spotify: could not load module: {e}')
        return out
    api = getattr(mod, 'spotify_api', None)

    def isrc_by_id(tid):
        try:
            if api and api._init_web_api_client() and getattr(api, 'client', None):
                tr = api.client.track(str(tid)) or {}
                return (tr.get('external_ids') or {}).get('isrc')
        except Exception:
            pass
        return None

    def from_tracklist(info):
        for t in (getattr(info, 'tracks', None) or []):
            isrc = getattr(getattr(t, 'tags', None), 'isrc', None)
            if not isrc:
                tid = getattr(t, 'id', None) or (t if isinstance(t, str) else None)
                isrc = isrc_by_id(tid) if tid else None
            if isrc:
                out.add(isrc.strip().upper())

    try:
        if mtype == 'track':
            i = isrc_by_id(mid)
            if i:
                out.add(i.strip().upper())
        elif mtype == 'album':
            from_tracklist(call_timeout(lambda: mod.get_album_info(str(mid)), 60,
                                        default=None, label='spotify album'))
        elif mtype == 'playlist':
            from_tracklist(call_timeout(lambda: mod.get_playlist_info(str(mid)), 120,
                                        default=None, label='spotify playlist'))
        elif mtype == 'artist':
            info = call_timeout(lambda: mod.get_artist_info(str(mid)), 90,
                                default=None, label='spotify artist')
            album_ids = [a if isinstance(a, str) else (a.get('id') if isinstance(a, dict) else None)
                         for a in (getattr(info, 'albums', None) or [])]
            for aid in [a for a in album_ids if a]:
                from_tracklist(call_timeout(lambda aid=aid: mod.get_album_info(str(aid)), 60,
                                            default=None, label=f'spotify album {aid}'))
    except Exception as e:
        log(f'  spotify: metadata read failed: {e}')
    return out


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
# Per-track OUTCOMES of an orpheus run, keyed by the numeric id orpheus prints.
# We detect PRESENCE explicitly (downloaded, or skipped/already in library) and let
# the caller treat every submitted track that is NOT present as failed. This is
# parsed from captured stdout because:
#   - orpheus exits 0 even when it logs "=== ERRORS ===", so the return code is not
#     a reliable success signal;
#   - some failures ("Track unavailable", "not available in your country", a 404 /
#     dead id) print no usable track id at all, so we can't match them positively.
# Detecting success and subtracting is therefore the only robust, walk-free signal.
_TRACK_START_RE = re.compile(r'=== Downloading track .*\((\d+)\) ===')
_PRESENT_MARKERS = ('Track skipped', 'already in library', 'Track file already exists')


def outcomes_from_output(text):
    """(downloaded, present) sets of track ids for an orpheus run:
    downloaded = reached 'Track completed'; present = skipped / already in library.
    Any submitted id in neither set failed (dead id, unavailable, flac-omit, etc.)."""
    downloaded, present = set(), set()
    cur = None
    for line in (text or '').splitlines():
        m = _TRACK_START_RE.search(line)
        if m:
            cur = m.group(1)
            continue
        if cur is None:
            continue
        if 'Track completed' in line:
            downloaded.add(cur)
            cur = None
        elif any(s in line for s in _PRESENT_MARKERS):
            present.add(cur)
            cur = None
    return downloaded, present


def run_orpheus(urls, dlq, out_path):
    """Run orpheus.py for these track URLs. Streams orpheus output live AND
    captures it, so the caller can tell which tracks failed to resolve (a 404 /
    dead id writes no error.txt). Returns (returncode, captured_stdout).
    `-u` keeps the child unbuffered so the tee stays live during long runs."""
    cmd = [sys.executable, '-u', 'orpheus.py', *urls, '-q', dlq]
    if out_path:
        cmd += ['-o', out_path]
    # Force the child to emit utf-8 on its (now piped) stdout so unicode titles
    # (accents, full-width chars) match our decode and don't turn into U+FFFD.
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    p = subprocess.Popen(cmd, cwd=SCRIPT_DIR, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding='utf-8', errors='replace', bufsize=1, env=env)
    chunks = []
    for line in p.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        chunks.append(line)
    p.wait()
    return p.returncode, ''.join(chunks)


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
        description='Download an artist/album/playlist/track as best-quality FLAC across '
                    'Tidal/Deezer/Qobuz (matched by ISRC).')
    ap.add_argument('url', help='Tidal/Deezer/Qobuz/Spotify URL: artist, album, playlist or track. '
                                '(Spotify is a metadata-only source: audio comes from the other services.)')
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
    ap.add_argument('--all-album-tracks', action='store_true',
                    help='(artist links) keep every track on the artist\'s albums, including '
                         'other artists\' tracks on compilations. Default: only tracks the artist performs.')
    ap.add_argument('--own-albums-only', dest='own_albums_only', action='store_true', default=None,
                    help='(artist links) skip whole albums not credited to the artist (compilations, '
                         'and collab/feature albums where they are not the album artist).')
    ap.add_argument('--all-albums', dest='own_albums_only', action='store_false',
                    help='(artist links) consider every album in the discography (overrides '
                         'own_albums_only from settings).')
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
    own_albums = cfg['own_albums_only'] if args.own_albums_only is None else args.own_albums_only
    out_path = args.output or (settings.get('global', {}).get('general', {}).get('download_path'))
    if out_path:
        out_path = out_path.rstrip('/\\') or out_path

    src_service, mtype, mid = parse_media_url(args.url)
    # Only real download services (qobuz/tidal/deezer) join the candidate pool.
    # A Spotify source is metadata-only: it supplies ISRCs, never FLAC.
    if src_service in ALL_SERVICES and src_service not in active:
        active = [src_service] + active
        prefer = [src_service] + [s for s in prefer if s != src_service]
    log(f'Source: {src_service} {mtype} {mid}')
    log(f'Target quality: {target} (download @ {dlq}); services: {", ".join(active)}; '
        f'{"credited=" + str(credited) + "; " if mtype == "artist" else ""}'
        f'dedup={not args.no_dedup and cfg["dedup_with_library"]}')

    os.chdir(SCRIPT_DIR)
    core = Core()
    try:
        union = {}  # isrc -> {service: (id, bd, sr)}

        # A Spotify artist (or any Spotify link) has no FLAC and no enumerator, so
        # it always goes through the ISRC-probe path below, never the union path.
        if mtype == 'artist' and src_service in ALL_SERVICES:
            # ---- artist name (cheap; for logging + name-search fallback) ----
            artist_name = artist_name_of(core, src_service, mid)
            if artist_name:
                log(f'Artist: {artist_name}')

            # ---- enumerate the source discography ----
            log(f'\nEnumerating {src_service} discography...')
            per_service = {src_service: ENUMERATORS[src_service](
                core.session(src_service), mid, credited, args.max_albums,
                not args.all_album_tracks, own_albums, artist_name)}
            log(f'  {src_service}: {len(per_service[src_service])} recordings with ISRC')

            src_set = set(per_service[src_service].keys())
            sample_isrcs = list(src_set)[:12]

            # ---- resolve + enumerate the other services ----
            for svc in active:
                if svc == src_service:
                    continue
                log(f'\nResolving artist on {svc}...')
                aid, method = resolve_artist(core, svc, sample_isrcs, artist_name)
                if not aid:
                    continue
                log(f'Enumerating {svc} discography...')
                mp = ENUMERATORS[svc](core.session(svc), aid, credited, args.max_albums,
                                      not args.all_album_tracks, own_albums, artist_name)
                # Verify a name-resolved artist really is the same person: its ISRCs
                # must overlap the source. Guards the union against a same-name match.
                if method == 'name' and src_set and mp:
                    overlap = len(src_set & set(mp.keys()))
                    if overlap == 0:
                        log(f'  {svc}: 0 ISRC overlap with source -> likely the wrong '
                            f'"{artist_name}"; discarding {len(mp)} tracks.')
                        continue
                    log(f'  {svc}: {overlap} shared ISRC(s) with source (name match verified)')
                per_service[svc] = mp
                log(f'  {svc}: {len(mp)} recordings with ISRC')

            for svc, mp in per_service.items():
                for isrc, cand in mp.items():
                    union.setdefault(isrc, {})[svc] = cand
            log(f'\nUnion: {len(union)} unique recordings across {", ".join(per_service)}')
        else:
            # ---- track / album / playlist: read the source ISRCs, then look each
            # recording up on every service and keep the best FLAC ----
            log(f'\nReading {mtype} {mid} from {src_service}...')
            isrcs = source_isrcs(core, src_service, mtype, mid)
            log(f'  {len(isrcs)} recording(s) with ISRC in the {mtype}')
            for i, isrc in enumerate(sorted(isrcs), 1):
                for svc in active:
                    cand = probe_isrc(core, svc, isrc)
                    if cand:
                        union.setdefault(isrc, {})[svc] = cand
                if len(isrcs) > 25 and i % 25 == 0:
                    log(f'  probed {i}/{len(isrcs)} ISRCs across services...')
            log(f'\nChecked {len(union)}/{len(isrcs)} recording(s) available on {", ".join(active)}')

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
                # target=lossless always downloads 16-bit even if the catalog is hi-res
                shown = '16bit/44.1kHz' if target == 'lossless' else f'{bd}bit/{sr}kHz'
                log(f'  {isrc}  -> {svc} @ {dlq} ({shown})  {track_url(svc, tid)}')
            if len(plan) > 200:
                log(f'  ... and {len(plan) - 200} more')
            return

        if not plan:
            log('Nothing to download.')
            return

        # ---- download: one orpheus.py call per (primary service, dlq) batch ----
        order_by_isrc = dict(plan)
        by_service = {}  # svc -> list of (isrc, tid, url), in plan order
        for isrc, order in plan:
            svc = order[0]
            tid = str(union[isrc][svc][0])
            by_service.setdefault(svc, []).append((isrc, tid, track_url(svc, tid)))

        # A primary track is FAILED unless orpheus reports it present (downloaded or
        # already in library). Detected from captured stdout -> catches every failure
        # mode (404 / dead id / flac-omit / "Track unavailable" / region lock) with
        # no library walk and without trusting orpheus' always-zero exit code.
        failed_direct = set()
        for svc in [s for s in prefer if s in by_service]:
            entries = by_service[svc]
            log(f'\n=== Downloading {len(entries)} track(s) from {svc} @ {dlq} ===')
            for i in range(0, len(entries), 40):
                chunk = entries[i:i + 40]
                _rc, out = run_orpheus([u for _i, _t, u in chunk], dlq, out_path)
                dl, pres = outcomes_from_output(out)
                ok = dl | pres
                for isrc, tid, _u in chunk:
                    if tid not in ok:
                        failed_direct.add(isrc)

        # ---- cross-service fallback: retry each failed ISRC on its next-best
        # service, in preference order, until one has it (downloaded or present). ----
        failed = failed_direct
        retry = [(isrc, order[1:]) for isrc, order in plan
                 if isrc in failed and len(order) > 1]
        recovered = set()          # newly downloaded from a fallback service
        present_elsewhere = set()  # already in the library (confirmed while retrying)
        if retry:
            log(f'\n=== ISRC fallback: {len(retry)} track(s) failed on best source; '
                f'retrying on next-best service ===')
            for isrc, alts in retry:
                for svc in alts:
                    tid = str(union[isrc][svc][0])
                    log(f'  retry {isrc} via {svc}')
                    _rc, out = run_orpheus([track_url(svc, tid)], dlq, out_path)
                    dl, pres = outcomes_from_output(out)
                    if tid in dl:
                        recovered.add(isrc)
                        break
                    if tid in pres:
                        present_elsewhere.add(isrc)
                        break

        # ---- visibility: tell the truth. orpheus logs "NO ERRORS" for tracks it
        # never downloaded, so a big batch would drop them silently otherwise. ----
        handled = recovered | present_elsewhere
        unrecovered = [isrc for isrc, _order in plan
                       if isrc in failed and isrc not in handled]
        if recovered:
            log(f'\n✔ recovered {len(recovered)} track(s) newly downloaded via '
                f'cross-service fallback')
        if present_elsewhere:
            log(f'\n• {len(present_elsewhere)} failed track(s) were already in the '
                f'library (confirmed during fallback)')
        if unrecovered:
            log(f'\n⚠️ {len(unrecovered)} track(s) could NOT be downloaded from the '
                f'tried service(s) for THIS account (restricted / unavailable here; '
                f'the FLAC may exist in the catalog or for another account):')
            for isrc in unrecovered:
                tried = '/'.join(order_by_isrc.get(isrc, [])) or '?'
                log(f'    {isrc}  (tried: {tried})')

        log('\nDone.')
    finally:
        core.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('\n^C - aborted')
