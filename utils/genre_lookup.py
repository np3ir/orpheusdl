"""Genre fallback for tracks whose service gave no genre (Tidal never does).

Order: Deezer public API by ISRC (track -> album -> album genres), then Spotify
(per-artist genres, coarse) when Deezer has nothing. Results are normalised to the
vocabulary already used in the library (Qobuz-style names) and cached on disk in
config/genre_cache.json, so every ISRC is asked at most once.

Settings (config/settings.json > global > general):
  genre_fallback   true  -> fill empty genres at tagging time (default true)
  genre_spotify    true  -> use Spotify when Deezer has none (default FALSE)
Spotify needs client_id/client_secret under modules > spotify (same as ISRC reads).
Spotify's Developer Terms (IV.3) forbid storing its content beyond what is strictly
necessary, so Spotify-derived genres are kept in memory only (never in the disk
cache) and the source is OFF by default. Enabling it is the user's decision; note
that tagging a file with them still stores them in the file.
Never raises: any failure just means "no genre found".
"""
import json
import os
import threading
import time
import types

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(SCRIPT_DIR, 'config')
CACHE_FILE = os.path.join(CONFIG_DIR, 'genre_cache.json')
NEGATIVE_TTL_S = 30 * 86400        # re-ask "no genre found" after 30 days
DEEZER_INTERVAL_S = 0.12           # Deezer allows ~50 requests / 5 s

# Deezer genre name -> library vocabulary (Qobuz style). Unlisted names pass through.
DEEZER_MAP = {
    'Rap/Hip Hop': 'Hip-Hop/Rap',
    'Latin Music': 'Latin',
    'Alternative': 'Alternative & Indie',
    'Indie Pop': 'Alternative & Indie',
    'Indie Rock': 'Alternative & Indie',
    'Indie Pop/Folk': 'Alternative & Indie',
    'Singer & Songwriter': 'Alternative & Indie',
    'Electro': 'Electronic',
    'Techno/House': 'Electronic',
    'Soul & Funk': 'Soul/Funk/R&B',
    'Contemporary R&B': 'R&B',
    'Rock & Roll/Rockabilly': 'Rock',
    'Dancehall/Ragga': 'Dancehall',
    'Films/Games': 'Film Soundtracks',
    'Film Scores': 'Film Soundtracks',
    'Brazilian Music': 'Brazil',
    'African Music': 'Africa',
    'Asian Music': 'Asia',
    'Kids': 'Children',
}   # Reggaeton / Salsa / Cumbia / Indian Music... already exist as-is in the library: kept

# Spotify micro-genre keyword -> library vocabulary; first match wins (order matters).
SPOTIFY_KEYWORDS = (
    (('reggaeton',), 'Reggaeton'),
    (('salsa',), 'Salsa'),
    (('k-pop',), 'K-Pop'),
    (('j-pop',), 'J-Pop'),
    (('latin', 'bachata', 'bachata', 'cumbia', 'merengue', 'bolero', 'mariachi', 'ranchera',
      'banda', 'norteno', 'tango', 'urbano', 'vallenato', 'tropical', 'mpb', 'bossa', 'sertanejo',
      'trova', 'flamenco', 'corrido'), 'Latin'),
    (('hip hop', 'rap', 'trap', 'drill', 'grime'), 'Hip-Hop/Rap'),
    (('r&b', 'soul', 'funk', 'motown', 'quiet storm'), 'Soul/Funk/R&B'),
    (('metal',), 'Metal'),
    (('punk', 'indie', 'alternative', 'emo', 'shoegaze'), 'Alternative & Indie'),
    (('rock', 'grunge'), 'Rock'),
    (('house', 'techno', 'edm', 'electro', 'trance', 'dubstep', 'drum and bass', 'synthwave', 'ambient',
      'electronic', 'idm'), 'Electronic'),
    (('dance', 'disco'), 'Dance'),
    (('country', 'bluegrass', 'americana'), 'Country'),
    (('jazz', 'bebop', 'swing'), 'Jazz'),
    (('classical', 'orchestra', 'baroque', 'opera', 'piano'), 'Classical'),
    (('reggae', 'dancehall', 'ska', 'dub'), 'Reggae'),
    (('blues',), 'Blues'),
    (('folk', 'singer-songwriter'), 'Folk'),
    (('gospel', 'christian', 'worship', 'ccm'), 'Christian'),
    (('soundtrack', 'score', 'video game'), 'Film Soundtracks'),
    (('afro', 'world', 'bollywood', 'arab', 'celtic'), 'World'),
    (('pop',), 'Pop'),
)


def _settings():
    try:
        with open(os.path.join(CONFIG_DIR, 'settings.json'), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def normalize_deezer(name):
    name = str(name or '').strip()
    return DEEZER_MAP.get(name, name)


def normalize_spotify(genres):
    """Collapse Spotify micro-genres to library vocabulary; keep first-seen order, no repeats."""
    out = []
    for g in genres or []:
        low = str(g).lower()
        hit = None
        for words, label in SPOTIFY_KEYWORDS:
            if any(w in low for w in words):
                hit = label
                break
        if hit and hit not in out:
            out.append(hit)
    return out


class GenreLookup:
    def __init__(self, deezer_get=None, spotify=None, use_spotify=True, cache_path=CACHE_FILE,
                 interval=DEEZER_INTERVAL_S, sleep=time.sleep):
        self._deezer_get = deezer_get
        self._spotify = spotify
        self.use_spotify = use_spotify
        self.cache_path = cache_path
        self.interval = interval
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._dirty = 0
        self._mem = {}              # Spotify-derived results: this process only, never persisted
        self._cache = self._load()

    # ---- cache -------------------------------------------------------------
    def _load(self):
        try:
            with open(self.cache_path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self):
        with self._lock:
            if not self._dirty:
                return
            try:
                os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                tmp = f'{self.cache_path}.{os.getpid()}.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self._cache, f, ensure_ascii=False)
                os.replace(tmp, self.cache_path)
                self._dirty = 0
            except Exception:
                pass

    def _remember(self, key, genres):
        with self._lock:
            self._cache[key] = {'g': list(genres), 't': int(time.time())}
            self._dirty += 1
            flush = self._dirty >= 25
        if flush:
            self.save()

    def _cached(self, key):
        with self._lock:
            row = self._cache.get(key)
        if not isinstance(row, dict):
            return None
        if not row.get('g') and time.time() - float(row.get('t') or 0) > NEGATIVE_TTL_S:
            return None  # stale "nothing found" -> ask again
        return list(row.get('g') or [])

    # ---- sources -----------------------------------------------------------
    def _deezer(self, path):
        if self._deezer_get is None:
            from utils import deezer_public
            self._deezer_get = deezer_public.get
        with self._lock:
            wait = self._last_call + self.interval - time.monotonic()
            self._last_call = time.monotonic() + max(0.0, wait)
        if wait > 0:
            self._sleep(wait)
        return self._deezer_get(path)

    def deezer_genres(self, isrc):
        try:
            track = self._deezer('/track/isrc:' + isrc)
            album_id = ((track or {}).get('album') or {}).get('id')
            if not album_id:
                return []
            key = f'alb:{album_id}'
            hit = self._cached(key)
            if hit is not None:
                return hit
            album = self._deezer(f'/album/{album_id}') or {}
            names = [normalize_deezer(g.get('name')) for g in ((album.get('genres') or {}).get('data') or [])
                     if g.get('name')]
            names = list(dict.fromkeys(n for n in names if n))
            self._remember(key, names)
            return names
        except Exception:
            return []

    def spotify_genres(self, isrc):
        if not (self.use_spotify and self._spotify):
            return []
        try:
            raw = self._spotify.artist_genres_by_isrc(isrc)
        except Exception:
            return []
        return normalize_spotify(raw) if raw else []

    # ---- public ------------------------------------------------------------
    def genres(self, isrc):
        """Genre list for this ISRC ([] when none found). Cached on disk."""
        isrc = str(isrc or '').strip().upper()
        if not isrc:
            return []
        hit = self._cached('isrc:' + isrc)
        if hit is not None:
            return hit
        if isrc in self._mem:
            return list(self._mem[isrc])
        found = self.deezer_genres(isrc)
        if found:
            self._remember('isrc:' + isrc, found)
            return found
        found = self.spotify_genres(isrc)
        if found:
            self._mem[isrc] = list(found)        # Spotify content: not written to the disk cache
        else:
            self._remember('isrc:' + isrc, [])   # "nothing found" is our own result, safe to cache
        return found


_shared = None
_shared_lock = threading.Lock()


def shared():
    """Process-wide lookup built from settings.json, or None when disabled."""
    global _shared
    with _shared_lock:
        if _shared is not None:
            return _shared or None
        general = (_settings().get('global') or {}).get('general') or {}
        if not general.get('genre_fallback', True):
            _shared = False
            return None
        spotify = None
        if general.get('genre_spotify', False):
            try:
                from utils.spotify_isrc import SpotifyIsrcLookup
                cfg = (_settings().get('modules') or {}).get('spotify') or {}
                if cfg.get('client_id') and cfg.get('client_secret'):
                    spotify = SpotifyIsrcLookup(types.SimpleNamespace(config=cfg), CONFIG_DIR,
                                                print_fn=lambda *a, **k: None)
            except Exception:
                spotify = None
        _shared = GenreLookup(spotify=spotify, use_spotify=spotify is not None)
        import atexit
        atexit.register(_shared.save)
        return _shared


def fill_genres(track_info):
    """Set track_info.tags.genres from the fallback when it is empty. Never raises."""
    try:
        tags = track_info.tags
        if tags.genres or not tags.isrc:
            return False
        lookup = shared()
        if lookup is None:
            return False
        found = lookup.genres(tags.isrc)
        if found:
            tags.genres = found
            return True
    except Exception:
        pass
    return False
