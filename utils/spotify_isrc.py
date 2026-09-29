"""ISRC lookup for Spotify tracks without burning the Spotify Web API quota.

Spotify is metadata-only for abq / orpheus.py: we only need each track's ISRC so
the exact recording can be downloaded elsewhere. The Web API (Client Credentials)
gives a dev-mode app a small quota, shared by all apps of the developer account;
one call per track on a 400-track playlist exhausts it and Spotify answers
``429 QUOTA_EXCEEDED`` with a Retry-After of hours. This module protects it:

  1. Playlists are read with GET /v1/playlists/{id}/items (get-playlists-items in
     the OpenAPI spec): 50 full track objects (with external_ids.isrc) per call,
     so a 416-track playlist costs 9 calls instead of 416. It sits in a different
     quota bucket than GET /v1/tracks/{id}. The spec documents this endpoint as
     owner/collaborator-only (403 otherwise); a 403 there only disables the bulk
     read and the per-track lookups take over.
  2. ISRCs are kept in memory for the current run only (Spotify Developer Terms:
     no caching beyond immediate use). A cache file left by an older version
     (config/spotify_isrc_cache.json) is deleted.
  3. Throttle between live calls.
  4. 429 (Spotify's rule: exponential backoff, respect Retry-After, no tight
     loops): wait max(Retry-After, 1 s * 2^n) and retry, up to 5 times. If
     Retry-After is longer than 10 minutes (a QUOTA_EXCEEDED block lasts hours)
     or the retries run out, that bucket stops for the run; the block is
     remembered per bucket (config/spotify_blocked_until.json, no Spotify
     content in it) so no call is made before Retry-After has passed, even in a
     new run. Every message shows what Spotify sent (status, reason, message,
     Retry-After).
  5. 401 (bad/expired token): renew the 1 h Client Credentials token once.
     403 on track lookups / a second 401: stop. 5xx and network errors: retried
     with exponential backoff (1 s, 2 s, 4 s); repeated failures stop the bucket.
"""
import json
import os
import time

import requests

_SPOTIFY_INTERVAL = 0.35    # seconds between live Web API calls
_PAGE_LIMIT = 50            # QueryLimit maximum in the OpenAPI spec
_BACKOFF = (1, 2, 4)        # seconds, for 5xx / network errors
_429_BASE = 1               # seconds; 429 backoff is 1, 2, 4, 8, 16 (or Retry-After if longer)
_429_RETRIES = 5
_429_MAX_WAIT = 600         # longer Retry-After -> stop the bucket instead of sleeping for hours
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
        self.block_path = os.path.join(config_dir, 'spotify_blocked_until.json')
        self.cache = {}         # track id -> ISRC, this run only
        try:
            os.remove(os.path.join(config_dir, 'spotify_isrc_cache.json'))  # older versions
        except OSError:
            pass
        self.stopped_by = {b: None for b in _BUCKETS}   # bucket -> reason, once disabled
        self.stats = {'spotify': 0, 'missing': 0}
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
            self.stats['spotify'] += 1
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
        """Read the ISRCs of a whole playlist, 50 tracks per call, for this run.
        Returns how many ISRCs were read, or None if the playlist could not be read
        completely (what was read is kept; the caller falls back to per-track
        lookups for the rest)."""
        if self.stopped_by['playlists']:
            return None
        url = f'{_API}/playlists/{playlist_id}/items?limit={_PAGE_LIMIT}'
        got = pages = 0
        while url:
            ok, page = self._call('playlists', lambda url=url: self._get(url))
            if not ok or not isinstance(page, dict):
                return None
            pages += 1
            for row in page.get('items') or []:
                tr = (row or {}).get('item') or {}   # PlaylistTrackObject.item (`track` is deprecated)
                if tr.get('type') != 'track':
                    continue                          # episodes / removed items carry no ISRC
                tid, isrc = tr.get('id'), _isrc_of(tr)
                if tid and isrc:
                    self._remember(str(tid), isrc)
                    got += 1
            url = page.get('next')
        self.print(f'  spotify: {got} ISRC(s) read from the playlist in {pages} call(s) (data: Spotify Web API)')
        return got

    def summary(self):
        s = self.stats
        return (f'ISRC (data: Spotify Web API): {s["spotify"]} read, {s["missing"]} without ISRC')

    def save(self):
        """Kept for callers; nothing is written (no caching beyond this run)."""

    # --------------------------------------------------------------- spotify
    def _get(self, url):
        r = requests.get(url, headers={'Authorization': f'Bearer {self.api.client.token}'}, timeout=20)
        r.raise_for_status()
        return r.json()

    def _call(self, bucket, fn):
        """Run one Web API call. Returns (ok, json); (True, None) on 404."""
        renewed = False
        backoff = list(_BACKOFF)
        n429 = 0
        while True:
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
                    n429 += 1
                    if self._on_429(bucket, resp, n429):
                        continue
                    return False, None
                d = _detail(_error_of(resp))
                if code == 401 and not renewed:
                    # Bad or expired token. Client Credentials tokens last 1 h and have
                    # no refresh token: drop the client so a new token is requested.
                    renewed = True
                    self.api.client = None
                    continue
                if code == 403 and bucket == 'playlists':
                    # Spec: get-playlists-items is owner/collaborator-only (403 otherwise).
                    self._stop('playlists', f'Spotify Web API 403{" " + d if d else ""} on '
                                            f'{_BUCKETS["playlists"]}; reading its tracks one by one instead.')
                    return False, None
                if code in (401, 403):
                    self._stop_all(f'Spotify Web API {code}{" " + d if d else ""}. In dev mode the app '
                                   f'owner needs an active Premium subscription; check client_id/client_secret.')
                    return False, None
                if code == 404:
                    return True, None
                if code == 400:
                    self._stop(bucket, f'Spotify Web API 400{" " + d if d else ""} on {_BUCKETS[bucket]}.')
                    return False, None
                if code and code >= 500 and backoff:
                    time.sleep(backoff.pop(0))
                    continue
                self._transient(bucket, f'HTTP {code}{" " + d if d else ""}')
                return False, None
            except Exception as e:
                if backoff:
                    time.sleep(backoff.pop(0))
                    continue
                self._transient(bucket, str(e))
                return False, None
            self._failures[bucket] = 0
            return True, data

    def _on_429(self, bucket, resp, n):
        """Handle the n-th consecutive 429 of a call. Waits and returns True to retry,
        or stops the bucket and returns False. Messages carry what Spotify sent, e.g.
        {"error":{"status":429,"message":"Too many requests","reason":"QUOTA_EXCEEDED"}}
        with ``Retry-After: 16008``."""
        try:
            retry = int((resp.headers or {}).get('Retry-After'))
        except (TypeError, ValueError):
            retry = None
        err = _error_of(resp)
        status = err.get('status') or 429
        what = (f'Spotify Web API {status} {_detail(err) or "Too many requests"} on {_BUCKETS[bucket]} '
                f'({"Retry-After: %d s" % retry if retry is not None else "no Retry-After sent"})')
        wait = max(retry or 0, _429_BASE * 2 ** (n - 1))
        if n <= _429_RETRIES and wait <= _429_MAX_WAIT:
            self.print(f'  spotify: {what}; waiting {wait} s before retry {n}/{_429_RETRIES}...')
            time.sleep(wait)
            return True
        if retry is not None:
            until = time.time() + retry
            blocks = self._blocks()
            blocks[bucket] = {'until': until, 'status': status, 'reason': err.get('reason'),
                              'message': err.get('message'), 'retry_after': retry}
            self._write(self.block_path, blocks)
            why = (f'Retry-After is {_fmt_wait(retry)}, until '
                   f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(until))}; not calling it before then'
                   if wait > _429_MAX_WAIT else f'still limited after {_429_RETRIES} retries')
        else:
            why = f'still limited after {_429_RETRIES} retries'
        self._stop(bucket, f'{what}. Stopped: {why}.')
        return False

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
        self.cache[tid] = isrc

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
