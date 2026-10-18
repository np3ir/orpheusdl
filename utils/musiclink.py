"""Translate a link from any streaming platform into something abq can read, via the
MusicLink API (https://musiclink.one/docs).

abq only reads Tidal / Deezer / Qobuz (and Spotify metadata). For a track, album or
artist link from Apple Music, YouTube Music, Amazon Music, Pandora, SoundCloud...,
MusicLink returns the recording's ISRC and its links on other platforms, so abq can
continue the normal way (by ISRC) instead of guessing from titles.

Key: free account at musiclink.one (300 requests / month, 10 / min, personal use).
Put it in config/settings.json > modules > musiclink > api_key, or in the
MUSICLINK_API_KEY environment variable. Never printed or logged.

Results are cached on disk (config/musiclink_cache.json) so a link is only ever
asked once; failed requests do not count against the allowance. Never raises.
"""
import json
import os
import threading
import time
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(SCRIPT_DIR, 'config', 'musiclink_cache.json')
BASE = 'https://api.musiclink.one'
CACHE_TTL_S = 90 * 86400
# Hosts abq reads itself: no translation needed.
NATIVE_HOSTS = ('tidal.com', 'deezer.com', 'qobuz.com', 'spotify.com')
PLATFORM_KEYS = ('qobuz', 'tidal', 'deezer')   # MusicLink platform keys abq can download from


class MusicLinkError(Exception):
    pass


def is_foreign(url):
    """True when the URL is on a platform abq does not read natively."""
    u = urlparse(url if '://' in url else 'https://' + url)
    host = (u.netloc or '').lower()
    return bool(host) and not any(h in host for h in NATIVE_HOSTS)


def api_key(settings=None):
    key = os.environ.get('MUSICLINK_API_KEY', '').strip()
    if not key and settings:
        key = str(((settings.get('modules') or {}).get('musiclink') or {}).get('api_key') or '').strip()
    return key


class MusicLink:
    def __init__(self, key, http=None, cache_path=CACHE_FILE, interval=6.5, timeout=45,
                 sleep=time.sleep, retries=3):
        self._key = key
        self._http = http
        self.cache_path = cache_path
        self.interval = interval         # 10 requests / minute on the free plan
        self.timeout = timeout           # the first lookup of an item can take 10-15 s
        self._sleep = sleep
        self.retries = retries
        self._lock = threading.Lock()
        self._last = 0.0
        self._cache = self._load()

    # ---- cache -------------------------------------------------------------
    def _load(self):
        try:
            with open(self.cache_path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp = f'{self.cache_path}.{os.getpid()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._cache, f, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except Exception:
            pass

    # ---- API ---------------------------------------------------------------
    def _get(self, path, params):
        http = self._http
        if http is None:
            import requests as http
        last = 'no attempt'
        for attempt in range(self.retries):
            with self._lock:
                wait = self._last + self.interval - time.monotonic()
                self._last = time.monotonic() + max(0.0, wait)
            if wait > 0:
                self._sleep(wait)
            try:
                resp = http.get(BASE + path, params=params, timeout=self.timeout,
                                headers={'Authorization': f'Bearer {self._key}'})
            except Exception as e:
                last = f'network error: {e.__class__.__name__}'
                self._sleep(3)
                continue
            code = resp.status_code
            if code == 200:
                try:
                    return resp.json()
                except Exception:
                    raise MusicLinkError('invalid JSON from MusicLink')
            if code == 401:
                raise MusicLinkError('MusicLink rejected the API key (401): check or renew it.')
            if code in (400, 404, 422):
                return None                      # nothing matched / cannot be handled: final
            if code == 429:
                try:
                    delay = float(resp.headers.get('Retry-After') or 0) or 15.0
                except Exception:
                    delay = 15.0
                last = f'rate limited (retry in {delay:.0f}s)'
                self._sleep(min(delay, 90.0))
                continue
            last = f'HTTP {code}'
            self._sleep(3)
        raise MusicLinkError(f'MusicLink request failed: {last}')

    def resolve(self, q):
        """First item MusicLink returns for a link/ISRC/UPC, or None. Cached."""
        key = 'r:' + q
        row = self._cache.get(key)
        if isinstance(row, dict) and time.time() - float(row.get('t') or 0) < CACHE_TTL_S:
            return row.get('item')
        body = self._get('/v2/resolve', {'q': q})
        item = None
        if isinstance(body, dict) and body.get('success'):
            data = body.get('data')
            if isinstance(data, list) and data:
                item = data[0]
            elif isinstance(data, dict):
                item = data
        if item is not None:
            keep = {k: item.get(k) for k in ('type', 'title', 'artist', 'isrc', 'duration_ms', 'platforms')}
            self._cache[key] = {'t': int(time.time()), 'item': keep}
            self._save()
            return keep
        return None


def pick_link(item, prefer=PLATFORM_KEYS, accept=None):
    """First platform link (in `prefer` order) abq can read. `accept(url)` may veto a URL."""
    plats = (item or {}).get('platforms') or {}
    for p in prefer:
        url = plats.get(p)
        if url and (accept is None or accept(url)):
            return p, url
    return None, None


def translate(url, client, prefer=PLATFORM_KEYS, accept=None):
    """Return {'url', 'isrc', 'type', 'title', 'artist', 'via'} or None when nothing matched.

    'url' is a Qobuz/Tidal/Deezer link abq can read; when only an ISRC is known for a track
    (no link on those platforms), 'url' is None and the ISRC alone drives the download."""
    item = client.resolve(url)
    if not item:
        return None
    service, link = pick_link(item, prefer, accept)
    isrc = str(item.get('isrc') or '').strip().upper() or None
    if not link and not (item.get('type') == 'track' and isrc):
        return None
    return {'url': link, 'isrc': isrc, 'type': item.get('type'), 'title': item.get('title'),
            'artist': item.get('artist'), 'via': service or 'isrc'}
