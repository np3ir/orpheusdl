"""ISRC lookup for Spotify tracks without burning the Spotify Web API quota.

Spotify is metadata-only for abq / orpheus.py: we only need each track's ISRC so
the exact recording can be downloaded elsewhere. The Web API (Client Credentials)
gives a dev-mode app a small quota, shared by all apps of the developer account;
one call per track on a 400-track playlist exhausts it and Spotify answers
``429 QUOTA_EXCEEDED`` with a Retry-After of hours. This module protects it:

  1. Playlists are read with GET /v1/playlists/{id}/items: 100 full track objects
     (with external_ids.isrc) per call, so a 416-track playlist costs 5 calls
     instead of 416. That endpoint sits in a different quota bucket than
     GET /v1/tracks/{id}, so it keeps working while track lookups are blocked.
  2. On-disk cache (config/spotify_isrc_cache.json): a track is asked once, ever.
  3. Throttle between live calls.
  4. Any 429 stops that bucket. The block is remembered per bucket
     (config/spotify_blocked_until.json) and that bucket gets NO calls until it
     expires -- hammering a blocked app only extends the block. The message shows
     what Spotify sent (status, reason, message, Retry-After).
  5. 401: renew the 1 h Client Credentials token once; a second 401 or a 403
     stops everything. Repeated errors stop the bucket for the run.
"""
import json
import os
import time

import requests

_SPOTIFY_INTERVAL = 0.35    # seconds between live Web API calls
_API = 'https://api.spotify.com/v1'
_BUCKETS = {'tracks': 'track lookups (GET /tracks/{id})',
            'playlists': 'playlist reads (GET /playlists/{id}/items)'}


def _fmt_wait(seconds):
    seconds = int(max(0, seconds))
    h, m = divmod(seconds // 60, 60)
    return f'{h}h {m:02d}m' if h else f'{max(m, 1)}m'


def _error_of(resp):
    """The ``error`` object of a Web API error body: {status, message, reason?}."""
    try:
        err = (resp.json() or {}).get('error') or {}
    except Exception:
        return {}
    return err if isinstance(err, dict) else {'message': str(err)}


def _detail(err):
    return ' / '.join(str(x) for x in (err.get('reason'), err.get('message')) if x)


def _isrc_of(track):
    return ((track.get('external_ids') or {}).get('isrc') or '').strip().upper() or None


class SpotifyIsrcLookup:
    """isrc(track_id) -> ISRC or None; prefetch_playlist(id) fills the cache in bulk."""

    def __init__(self, api, config_dir, print_fn=print):
        self.api = api
        self.print = print_fn
        self.cache_path = os.path.join(config_dir, 'spotify_isrc_cache.json')
        self.block_path = os.path.join(config_dir, 'spotify_blocked_until.json')
        self.cache = dict(self._load(self.cache_path).get('spotify') or {})
        self.stopped_by = {b: None for b in _BUCKETS}   # bucket -> reason, once disabled
        self.stats = {'cache': 0, 'spotify': 0, 'missing': 0}
        self._fresh = set()     # ids fetched live this run (counted as Web API, not cache)
        self._dirty = 0
        self._last = 0.0
        self._failures = {b: 0 for b in _BUCKETS}
        blocks = self._blocks()
        for bucket, block in blocks.items():
            until = float(block.get('until') or 0)
            if until > time.time():
                d = _detail(block)
                self._stop(bucket, f'Spotify Web API {_BUCKETS[bucket]} still blocked by an earlier '
                                   f'{block.get("status") or 429}{" " + d if d else ""}: '
                                   f'{_fmt_wait(until - time.time())} left (until '
                                   f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(until))}). Not calling it.')

    @property
    def stopped(self):
        """Reason track lookups are disabled (None while they still run)."""
        return self.stopped_by['tracks']

    # ---------------------------------------------------------------- public
    def isrc(self, track_id):
        tid = str(track_id or '')
        if not tid:
            self.stats['missing'] += 1
            return None
        if tid in self.cache:
            self.stats['spotify' if tid in self._fresh else 'cache'] += 1
            return self.cache[tid]
        found = None
        if not self.stopped:
            ok, tr = self._call('tracks', lambda: self.api.client.track(tid))
            found = _isrc_of(tr) if ok and isinstance(tr, dict) else None
        if found:
            self._remember(tid, found)
            self.stats['spotify'] += 1
        else:
            self.stats['missing'] += 1
        return found

    def prefetch_playlist(self, playlist_id):
        """Cache the ISRCs of a whole playlist, 100 tracks per call. Returns how many
        ISRCs were read, or None if the playlist could not be read completely (what
        was read is still cached; the caller falls back to per-track lookups)."""
        if self.stopped_by['playlists']:
            return None
        url = f'{_API}/playlists/{playlist_id}/items?limit=100'
        got = pages = 0
        try:
            while url:
                ok, page = self._call('playlists', lambda url=url: self._get(url))
                if not ok or not isinstance(page, dict):
                    return None
                pages += 1
                for row in page.get('items') or []:
                    tr = (row or {}).get('item') or (row or {}).get('track') or {}
                    tid, isrc = tr.get('id'), _isrc_of(tr)
                    if tid and isrc:
                        self._remember(str(tid), isrc)
                        got += 1
                url = page.get('next')
        finally:
            self.save()
        self.print(f'  spotify: {got} ISRC(s) read from the playlist in {pages} call(s)')
        return got

    def summary(self):
        s = self.stats
        return (f'ISRC: {s["cache"]} from cache, {s["spotify"]} from the Spotify Web API, '
                f'{s["missing"]} without ISRC')

    def save(self):
        if self._dirty:
            self._write(self.cache_path, {'spotify': self.cache})
            self._dirty = 0

    # --------------------------------------------------------------- spotify
    def _get(self, url):
        r = requests.get(url, headers={'Authorization': f'Bearer {self.api.client.token}'}, timeout=20)
        r.raise_for_status()
        return r.json()

    def _call(self, bucket, fn):
        """Run one Web API call. Returns (ok, json); (True, None) on 404."""
        for attempt in range(2):
            try:
                ready = self.api._init_web_api_client() and getattr(self.api, 'client', None)
            except Exception:
                ready = False
            if not ready:
                self._stop_all('Spotify Web API unavailable (set client_id/client_secret in '
                               'config/settings.json > modules > spotify).')
                return False, None
            wait = self._last + _SPOTIFY_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                data = fn()
            except requests.HTTPError as e:
                resp = e.response
                code = getattr(resp, 'status_code', None)
                if code == 429:
                    self._on_429(bucket, resp)
                    return False, None
                if code == 401 and attempt == 0:
                    # Client Credentials tokens last 1 h and have no refresh token:
                    # drop the cached client so _init_web_api_client() gets a new one.
                    self.api.client = None
                    continue
                if code in (401, 403):
                    d = _detail(_error_of(resp))
                    self._stop_all(f'Spotify Web API {code}{" " + d if d else ""}. In dev mode the app '
                                   f'owner needs an active Premium subscription; check client_id/client_secret.')
                    return False, None
                if code == 404:
                    return True, None
                self._transient(bucket, f'HTTP {code}')
                return False, None
            except Exception as e:
                self._transient(bucket, str(e))
                return False, None
            self._failures[bucket] = 0
            return True, data
        return False, None

    def _on_429(self, bucket, resp):
        """Stop the bucket on a 429 and report exactly what Spotify sent back, e.g.
        {"error":{"status":429,"message":"Too many requests","reason":"QUOTA_EXCEEDED"}}
        with ``Retry-After: 16008``."""
        try:
            retry = int((resp.headers or {}).get('Retry-After'))
        except (TypeError, ValueError):
            retry = None
        err = _error_of(resp)
        status = err.get('status') or 429
        detail = _detail(err) or 'Too many requests'
        if retry is not None:
            until = time.time() + retry
            blocks = self._blocks()
            blocks[bucket] = {'until': until, 'status': status, 'reason': err.get('reason'),
                              'message': err.get('message'), 'retry_after': retry}
            self._write(self.block_path, blocks)
            when = (f'Retry-After: {retry} s = {_fmt_wait(retry)}, '
                    f'until {time.strftime("%Y-%m-%d %H:%M", time.localtime(until))}')
        else:
            when = 'no Retry-After sent'
        self._stop(bucket, f'Spotify Web API {status} {detail} on {_BUCKETS[bucket]} ({when}). '
                           f'Stopped calling it.')

    def _transient(self, bucket, why):
        self._failures[bucket] += 1
        if self._failures[bucket] >= 5:
            self._stop(bucket, f'Spotify Web API keeps failing on {_BUCKETS[bucket]} ({why}); '
                               f'stopped for this run.')

    def _stop(self, bucket, reason):
        if not self.stopped_by[bucket]:
            self.stopped_by[bucket] = reason
            self.print(f'  spotify: {reason}')

    def _stop_all(self, reason):
        if not all(self.stopped_by.values()):
            self.print(f'  spotify: {reason}')
        for b in self.stopped_by:
            self.stopped_by[b] = self.stopped_by[b] or reason

    # ------------------------------------------------------------- storage
    def _remember(self, tid, isrc):
        self._fresh.add(tid)
        if self.cache.get(tid) != isrc:
            self.cache[tid] = isrc
            self._dirty += 1
            if self._dirty >= 100:
                self.save()

    def _blocks(self):
        data = self._load(self.block_path)
        if 'until' in data:     # single-bucket format written by the first version
            data = {'tracks': data}
        return {b: v for b, v in data.items() if b in _BUCKETS and isinstance(v, dict)}

    @staticmethod
    def _load(path):
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _write(path, data):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f'{path}.{os.getpid()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception:
            pass
