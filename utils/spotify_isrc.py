"""ISRC lookup for Spotify tracks without burning the Spotify Web API quota.

Spotify is metadata-only for abq / orpheus.py: we only need each track's ISRC so
the exact recording can be downloaded elsewhere. The Web API (Client Credentials)
gives a dev-mode app a small quota; one call per track on a 400-track playlist
exhausts it and Spotify answers ``429 QUOTA_EXCEEDED`` with a Retry-After of hours.
This module protects that quota:

  1. On-disk cache (config/spotify_isrc_cache.json): a track is asked once, ever.
  2. Throttle between live calls.
  3. Any 429 stops the lookups. The block is remembered
     (config/spotify_blocked_until.json) and NO Spotify calls are made until it
     expires -- hammering a blocked app only extends the block. 401/403 and
     repeated errors also stop the lookups for the run.
"""
import json
import os
import time

import requests

_SPOTIFY_INTERVAL = 0.35    # seconds between live Web API calls


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


class SpotifyIsrcLookup:
    """isrc(track_id) -> ISRC or None; cached, throttled, stops on 429."""

    def __init__(self, api, config_dir, print_fn=print):
        self.api = api
        self.print = print_fn
        self.cache_path = os.path.join(config_dir, 'spotify_isrc_cache.json')
        self.block_path = os.path.join(config_dir, 'spotify_blocked_until.json')
        self.cache = dict(self._load(self.cache_path).get('spotify') or {})
        self.stopped = None     # reason once lookups are disabled for this run
        self.stats = {'cache': 0, 'spotify': 0, 'missing': 0}
        self._dirty = 0
        self._last = 0.0
        self._failures = 0
        block = self._load(self.block_path)
        until = float(block.get('until') or 0)
        if until > time.time():
            detail = ' / '.join(str(x) for x in (block.get('reason'), block.get('message')) if x)
            self._stop(f'Spotify Web API still blocked by an earlier {block.get("status") or 429}'
                       f'{" " + detail if detail else ""}: {_fmt_wait(until - time.time())} left '
                       f'(until {time.strftime("%Y-%m-%d %H:%M", time.localtime(until))}). Not calling Spotify.')

    # ---------------------------------------------------------------- public
    def isrc(self, track_id):
        tid = str(track_id or '')
        if not tid:
            self.stats['missing'] += 1
            return None
        if tid in self.cache:
            self.stats['cache'] += 1
            return self.cache[tid]
        found = None if self.stopped else self._fetch(tid)
        if found:
            self.cache[tid] = found
            self._dirty += 1
            if self._dirty >= 25:
                self.save()
            self.stats['spotify'] += 1
        else:
            self.stats['missing'] += 1
        return found

    def summary(self):
        s = self.stats
        return (f'ISRC: {s["cache"]} from cache, {s["spotify"]} from the Spotify Web API, '
                f'{s["missing"]} without ISRC')

    def save(self):
        if self._dirty:
            self._write(self.cache_path, {'spotify': self.cache})
            self._dirty = 0

    # --------------------------------------------------------------- spotify
    def _fetch(self, tid):
        for attempt in range(2):
            try:
                ok = self.api._init_web_api_client() and getattr(self.api, 'client', None)
            except Exception:
                ok = False
            if not ok:
                self._stop('Spotify Web API unavailable (set client_id/client_secret in '
                           'config/settings.json > modules > spotify).')
                return None
            wait = self._last + _SPOTIFY_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                tr = self.api.client.track(tid) or {}
            except requests.HTTPError as e:
                resp = e.response
                code = getattr(resp, 'status_code', None)
                if code == 429:
                    self._on_429(resp)
                    return None
                if code == 401 and attempt == 0:
                    # Client Credentials tokens last 1 h and have no refresh token:
                    # drop the cached client so _init_web_api_client() gets a new one.
                    self.api.client = None
                    continue
                if code in (401, 403):
                    err = _error_of(resp)
                    detail = ' / '.join(str(x) for x in (err.get('reason'), err.get('message')) if x)
                    self._stop(f'Spotify Web API {code}{" " + detail if detail else ""}. In dev mode the app '
                               f'owner needs an active Premium subscription; check client_id/client_secret.')
                    return None
                if code == 404:
                    return None
                return self._transient(f'HTTP {code}')
            except Exception as e:
                return self._transient(str(e))
            self._failures = 0
            isrc = ((tr.get('external_ids') or {}).get('isrc') or '').strip().upper()
            return isrc or None
        return None

    def _on_429(self, resp):
        """Stop on a 429 and report exactly what Spotify sent back, e.g.
        {"error":{"status":429,"message":"Too many requests","reason":"QUOTA_EXCEEDED"}}
        with ``Retry-After: 16008``."""
        raw_retry = (resp.headers or {}).get('Retry-After')
        try:
            retry = int(raw_retry)
        except (TypeError, ValueError):
            retry = None
        err = _error_of(resp)
        status = err.get('status') or 429
        detail = ' / '.join(str(x) for x in (err.get('reason'), err.get('message')) if x) or 'Too many requests'
        if retry is not None:
            until = time.time() + retry
            self._write(self.block_path, {'until': until, 'status': status,
                                          'reason': err.get('reason'), 'message': err.get('message'),
                                          'retry_after': retry})
            when = (f'Retry-After: {retry} s = {_fmt_wait(retry)}, '
                    f'until {time.strftime("%Y-%m-%d %H:%M", time.localtime(until))}')
        else:
            when = 'no Retry-After sent'
        self._stop(f'Spotify Web API {status} {detail} ({when}). Stopped reading ISRCs.')

    def _transient(self, why):
        self._failures += 1
        if self._failures >= 5:
            self._stop(f'Spotify Web API keeps failing ({why}); stopped for this run.')
        return None

    def _stop(self, reason):
        if not self.stopped:
            self.stopped = reason
            self.print(f'  spotify: {reason}')

    # ------------------------------------------------------------- storage
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
