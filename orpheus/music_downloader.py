import logging
import os
import shutil
import unicodedata
import json
import uuid
import time
import re
import platform
import asyncio
import sys
import sqlite3
import threading
import traceback
from dataclasses import asdict, dataclass
from time import strftime, gmtime
from datetime import datetime
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed

# Third party imports
import ffmpeg
from ffmpeg import Error


# --- FIX: Import tqdm AFTER utils to avoid conflicts ---
try:
    from tqdm import tqdm as tqdm_progress
except ImportError:
    tqdm_progress = None

# Local imports
from orpheus.tagging import tag_file
from utils.models import *
from utils.utils import *
from utils.exceptions import *

# --- Modular Spotify Import ---
try:
    from modules.spotify.spotify_api import SpotifyRateLimitDetectedError
except ModuleNotFoundError:
    class SpotifyRateLimitDetectedError(Exception):
        pass

# ============================================================
# CONFIGURACIÓN PERSONALIZADA DE USUARIO
# ============================================================

# OPCIONAL: Define aquí la estructura si no quieres usar el settings.json.
# Si usas el truco de "|||" en settings.json, esto se ignora.
PLAYLIST_TRACK_STRUCTURE = "E:/{name}/{artists} - {title_clean}{explicit}{master: [m]}{dolby: [atmos]}"

# ============================================================
# CONSTANTES Y REGEX
# ============================================================

PLATFORM_COLORS = {
    "tidal": "\033[96m",
    "jiosaavn": "\x1b[96m",
    "apple music": "\033[91m",
    "beatport": "\033[92m",
    "beatsource": "\033[94m",
    "deezer": "\033[38;5;129m",
    "qobuz": "\033[34m",
    "soundcloud": "\033[38;5;208m",
    "spotify": "\033[32m",
    "napster": "\033[94m",
    "kkbox": "\033[36m",
    "idagio": "\033[35m",
    "bugs": "\033[31m",
    "nugs": "\033[31m"
}

RESET_COLOR = "\033[0m"

MAX_ARTISTS_LEN = 40
MAX_TITLE_LEN = 50
ASCII_ONLY = False

_WIN_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
_DRIVE_RE = re.compile(r"^[A-Za-z]:$")

_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}

# --- REGEX PARA LIMPIEZA DE TÍTULOS ---
_RE_ANTI_FEAT = re.compile(r"\s*\((?:f(?:ea)?t\.?|with|starring)\s+(.*?)\)", flags=re.IGNORECASE)
_RE_NORMALIZE = re.compile(r'[\W_]+')


# ============================================================
# UTILS GENERALES
# ============================================================

def get_colored_platform_name(service_name):
    if not service_name:
        return "Unknown"
    normalized_name = service_name.lower()
    color_code = PLATFORM_COLORS.get(normalized_name, "")
    if color_code:
        return f"{color_code}{service_name}{RESET_COLOR}"
    else:
        return service_name


def beauty_format_seconds(seconds: int) -> str:
    time_data = gmtime(seconds)
    time_format = "%Mm:%Ss"
    if time_data.tm_hour > 0:
        time_format = "%Hh:" + time_format
    return strftime(time_format, time_data)


def simplify_error_message(error_str: str) -> str:
    error_lower = error_str.lower()
    if any(phrase in error_lower for phrase in ['track is unavailable', 'track unavailable', 'unavailable']):
        return "This song is unavailable."
    if '"code":404' in error_str or '"code": 404' in error_str:
        return "This song is unavailable."
    if 'status code 404' in error_lower or 'error 404' in error_lower:
        return "This song is unavailable."
    if 'apple music' in error_lower:
        if 'unexpected error during download' in error_lower:
            if any(keyword in error_lower for keyword in ['ffmpeg', 'remux', 'processing', 'legacy remux', 'expected']):
                return "Apple Music streaming error (FFmpeg required for processing)"
            return "Apple Music download error"
        return "Apple Music error"
    if 'ffmpeg' in error_lower:
        return "Audio processing error (FFmpeg)"
    if len(error_str) > 80:
        return error_str[:77] + "..."
    return error_str


def json_enum_serializer(obj):
    if isinstance(obj, Enum):
        return obj.name
    raise TypeError(f'Object of type {obj.__class__.__name__} is not JSON serializable')


def _truncate(s: str, max_len: int) -> str:
    if not isinstance(s, str): s = str(s)
    s = s.strip()
    if len(s) <= max_len:
        return s
    return s[: max_len]


def sanitise_name(path: str) -> str:
    if path is None:
        return ""
    s = str(path).strip()
    s = unicodedata.normalize("NFC", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    replacements = {
        "/": "／", "\\": "＼", ":": "：", "*": "＊",
        "?": "？", '"': "＂", "<": "＜", ">": "＞", "|": "｜",
    }
    for char, repl in replacements.items():
        s = s.replace(char, repl)
    if ASCII_ONLY:
        s = s.encode("ascii", "ignore").decode("ascii", "ignore")
    s = _WIN_FORBIDDEN_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    if s.upper().split('.')[0] in _RESERVED_NAMES:
        s = f"_{s}"
    return s if s else "Unknown"


def _sanitize_segment(segment: str, index: int) -> str:
    s = (segment or "").strip()
    if index == 0 and _DRIVE_RE.match(s):
        return s.upper()
    return sanitise_name(s)


# ============================================================
# LÓGICA DE LIMPIEZA AVANZADA (ANTI-FEAT)
# ============================================================

def normalize_text(text: str) -> str:
    if not text: return ""
    return _RE_NORMALIZE.sub('', text).lower()

def clean_track_title_logic(track_title: str, artist_name: str) -> str:
    match = _RE_ANTI_FEAT.search(track_title)
    if match:
        feat_artist = match.group(1)
        simple_feat = normalize_text(feat_artist)
        simple_main_artist = normalize_text(artist_name)
        if len(simple_feat) > 2 and simple_feat in simple_main_artist:
            return track_title.replace(match.group(0), "").strip()
    return track_title


# ============================================================
# LÓGICA DE FORMATO Y TEMPLATES
# ============================================================

class Explicit:
    def __init__(self, val):
        self.val = val

    def __format__(self, fmt):
        if not self.val:
            return ""
        if "shortparens" in fmt: return " (explicit)"
        if "parens" in fmt: return " (explicit)"
        if "upper" in fmt: return "EXPLICIT" if "long" in fmt else "E"
        return " (explicit)"


class UserFormat:
    def __init__(self, val): self.val = val
    def __format__(self, fmt): return fmt if self.val else ""


def format_template_advanced(template: str, data: dict, with_asterisk_ext: bool = False) -> str:
    template = template.strip().lstrip('\ufeff').replace("\\", "/")
    if 'now' not in data:
        data['now'] = datetime.now()
    parts = template.split("/")
    rendered_parts = []
    
    is_unc = template.startswith("//") or template.startswith("\\\\")
    is_absolute_win = bool(_DRIVE_RE.match(parts[0])) if parts else False
    
    if is_unc: parts = [p for p in parts if p]
    
    for idx, part in enumerate(parts):
        try:
            rendered = part.format(**data)
        except Exception:
            rendered = part.replace(":", "-").replace("{", "(").replace("}", ")")
        
        if idx == 0 and is_absolute_win:
            rendered_parts.append(rendered.upper())
        else:
            seg_idx = idx if not is_unc else idx + 99
            rendered_parts.append(_sanitize_segment(rendered, seg_idx))
            
    path = "/".join(rendered_parts)
    if is_unc: path = "//" + path
    return path + ".*" if with_asterisk_ext else path


def clean_string_logic(text):
    if not text: return ""
    text = re.sub(r"\s*\(\s*(?:feat|ft|featuring|with|con|invitado|invitada|colaboraci[óo]n|prod|dueto)\.?\s+.*?\)", "",
                  text, flags=re.IGNORECASE)
    return text.strip()


def determine_release_type(track_info=None, album_info=None):
    valid_types = {'SINGLE', 'EP', 'ALBUM', 'COMPILATION', 'ANTHOLOGY'}
    sources = []
    if album_info: sources.append(album_info)
    if track_info: sources.append(track_info)
    for source in sources:
        if hasattr(source, 'type'):
            val = str(getattr(source, 'type', '')).upper().strip()
            if val in valid_types: return f"({val})"
    return "(ALBUM)"


def _normalize_for_compare(s: str) -> str:
    """Normalize string for fuzzy comparison: remove diacritics, lowercase, collapse spaces."""
    decomposed = unicodedata.normalize('NFD', s)
    stripped = ''.join(c for c in decomposed if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', stripped).lower().strip()


def _get_initials(name: str) -> str:
    if not name: return '#'
    ch = name.strip()[0].upper()
    decomposed = unicodedata.normalize('NFD', ch)
    base = ''.join(c for c in decomposed if unicodedata.category(c) != 'Mn').upper()
    return base if ('A' <= base <= 'Z') else '#'


def prepare_template_data(track_info: TrackInfo = None, album_info: AlbumInfo = None, extra_tags: dict = None) -> dict:
    data = {}
    if track_info:
        artists_list = track_info.artists if isinstance(track_info.artists, list) else [str(track_info.artists)]
        flat_artists = []
        for a in artists_list:
            flat_artists.extend(re.split(r'\s*[／/]\s*', str(a)))
        artists_list = [a for a in flat_artists if a]
        main_artist = artists_list[0] if artists_list else "Unknown Artist"
        original_title = track_info.name
        
        clean_title = clean_track_title_logic(original_title, ", ".join(artists_list))
        clean_title = clean_string_logic(clean_title)
        
        bit_depth = getattr(track_info, 'bit_depth', 0) or 0
        sample_rate = getattr(track_info, 'sample_rate', 0) or 0
        sample_rate_khz = f"{sample_rate / 1000:g}" if sample_rate else ""
        
        is_master = False
        if bit_depth > 16 or sample_rate > 48000: is_master = True
        
        is_dolby = False
        if track_info.codec:
            c_name = track_info.codec.name if hasattr(track_info.codec, 'name') else str(track_info.codec)
            if c_name in ['EAC3', 'AC4', 'AC3']: is_dolby = True

        t_tags = getattr(track_info, 'tags', None)
        release_date = (getattr(t_tags, 'release_date', None) or "") if t_tags else ""
        if release_date and len(release_date) > 10:
            release_date = release_date[:10]
        album_name_raw = getattr(t_tags, 'album', None) or getattr(track_info, 'album', None) or ""
        album_artist_raw = (getattr(t_tags, 'album_artist', None) or "") if t_tags else ""
        if not album_artist_raw:
            album_artist_raw = main_artist

        data.update({
            'name': original_title,
            'title': original_title,
            'title_clean': clean_title,
            'title_trunc': _truncate(original_title, MAX_TITLE_LEN),
            'artist': main_artist,
            'artists': " / ".join(str(a) for a in artists_list),
            'album_artist': album_artist_raw,
            'explicit': Explicit(track_info.explicit),
            'quality': f"({track_info.codec.name})" if track_info.codec else '',
            'master': UserFormat(is_master),
            'dolby': UserFormat(is_dolby),
            'bit_depth': bit_depth,
            'sample_rate_khz': sample_rate_khz,
            'year': release_date[:4] if len(release_date) >= 4 else "",
            'release_date': release_date,
            'album': album_name_raw,
            'album_clean': clean_string_logic(album_name_raw),
            'release': determine_release_type(track_info, album_info),
            'artist_initials': _get_initials(album_artist_raw),
        })
        if t_tags:
            data.update({
                'track_number': getattr(t_tags, 'track_number', 0),
                'total_tracks': getattr(t_tags, 'total_tracks', 0),
                'disc_number': getattr(t_tags, 'disc_number', 0),
                'total_discs': getattr(t_tags, 'total_discs', 0),
            })
    if album_info:
        r_year = str(album_info.release_year) if album_info.release_year else ""
        data.update({
            'album': album_info.name,
            'album_clean': clean_string_logic(album_info.name),
            'album_artist': album_info.artist,
            'year': r_year[:4] if len(r_year) >= 4 else r_year,
        })
    if extra_tags: data.update(extra_tags)
    return data


# ============================================================
# CLASE DOWNLOADER
# ============================================================

class Downloader:
    def __init__(self, settings, module_controls, oprinter, path, use_ansi_colors=True):
        self.global_settings = settings
        self.module_controls = module_controls
        self.oprinter = oprinter
        self.path = path
        self.service = None
        self.service_name = None
        self.download_mode = None
        self.indent_number = 0
        self.module_list = module_controls['module_list']
        self.module_settings = module_controls['module_settings']
        self.loaded_modules = module_controls['loaded_modules']
        self.load_module = module_controls['module_loader']
        self.full_settings = None
        self.use_ansi_colors = use_ansi_colors
        self.print = self.oprinter.oprint
        self.set_indent_number = self.oprinter.set_indent_number
        self.third_party_modules = {}

        # --- DB INIT ---
        self.db_path = os.path.join(os.getcwd(), 'archive.db')
        self.db_lock = threading.Lock() 
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_db()

    def _init_db(self):
        try:
            with self.db_lock:
                self.conn.execute('''CREATE TABLE IF NOT EXISTS downloads (
                                                id TEXT PRIMARY KEY,
                                                artist TEXT,
                                                title TEXT,
                                                album TEXT,
                                                quality TEXT,
                                                filename TEXT,
                                                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                                            )''')
                self.conn.commit()
        except Exception as e:
            logging.error(f"Failed to initialize database: {e}")

    def _check_db(self, track_id):
        try:
            with self.db_lock:
                cursor = self.conn.cursor()
                cursor.execute("SELECT id, filename FROM downloads WHERE id = ?", (str(track_id),))
                row = cursor.fetchone()
                if row is None:
                    return False
                filename = row[1]
                if filename and not os.path.exists(filename):
                    self.conn.execute("DELETE FROM downloads WHERE id = ?", (str(track_id),))
                    self.conn.commit()
                    return False
                return True
        except Exception:
            return False

    def _add_to_db(self, track_info, filename):
        try:
            artist = track_info.artists[0] if track_info.artists else "Unknown"
            title = track_info.name
            album = getattr(track_info, 'album', "Unknown")
            if hasattr(track_info, 'tags') and track_info.tags:
                album = getattr(track_info.tags, 'album', album)

            quality = "Unknown"
            if hasattr(track_info, 'codec') and track_info.codec:
                quality = str(track_info.codec.name)

            with self.db_lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO downloads (id, artist, title, album, quality, filename) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(track_info.id), str(artist), str(title), str(album), quality, str(filename)))
                self.conn.commit()
        except Exception as e:
            logging.error(f"Error adding to DB: {e}")

    def _get_status_symbols(self):
        GREEN, YELLOW, RED, GRAY, RESET = '\033[92m', '\033[33m', '\033[91m', '\033[90m', '\033[0m'
        if not self.use_ansi_colors:
            return {'success': '+', 'skip': '>', 'error': 'x', 'warning': '!', 'red_text': '', 'yellow_text': '', 'reset': ''}
        return {'success': f'{GREEN}+{RESET}', 'skip': f'{YELLOW}>{RESET}', 'error': f'{RED}x{RESET}',
                'warning': f'{YELLOW}!{RESET}', 'yellow_text': YELLOW, 'red_text': RED, 'reset': RESET}

    def create_temp_filename(self):
        temp_dir = os.path.abspath(os.path.join(os.getcwd(), 'temp'))
        os.makedirs(temp_dir, exist_ok=True)
        return os.path.join(temp_dir, str(uuid.uuid4()))

    _AUDIO_EXTENSIONS = ('.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav', '.aiff', '.mp4')

    # _normalize_for_compare defined at module level below

    def _track_file_exists(self, loc: str) -> Optional[str]:
        """Return the existing file path if a track already exists at this location
        with any audio extension — catches FLAC files when loc is .m4a and vice versa.
        Also matches files that differ only in diacritics (accents)."""
        if os.path.exists(loc):
            return loc
        base = os.path.splitext(loc)[0]
        clean_base = base.lstrip('\\\\?\\') if base.startswith('\\\\?\\') else base
        prefix = '\\\\?\\' if base.startswith('\\\\?\\') else ''

        # Exact extension match first
        for ext in self._AUDIO_EXTENSIONS:
            candidate = prefix + clean_base + ext
            if os.path.exists(candidate):
                return candidate

        # Fuzzy match: scan sibling files in the parent directory, ignoring diacritics
        parent = os.path.dirname(clean_base)
        target_name_norm = _normalize_for_compare(os.path.basename(clean_base))
        try:
            for fname in os.listdir(parent) if os.path.isdir(parent) else []:
                fbase, fext = os.path.splitext(fname)
                if fext.lower() in self._AUDIO_EXTENSIONS:
                    if _normalize_for_compare(fbase) == target_name_norm:
                        return prefix + os.path.join(parent, fname)
        except Exception:
            pass
        return None

    def _get_lyrics(self, track_id, track_info) -> Optional[LyricsInfo]:
        # 1. Check third party lyrics module
        lyrics_module_name = self.third_party_modules.get(ModuleModes.lyrics)
        
        if lyrics_module_name:
            self.print(f'Searching for lyrics on {lyrics_module_name}...', drop_level=1)
            # Use third party module
            try:
                # Search for the track on the lyrics service
                results = self.search_by_tags(lyrics_module_name, track_info)
                if results:
                    # Use the first result
                    lyrics_id = results[0].result_id
                    lyrics = self.loaded_modules[lyrics_module_name].get_track_lyrics(lyrics_id)
                    if lyrics:
                         self.print('Lyrics downloaded!', drop_level=1)
                         return lyrics
                self.print('No lyrics found on external provider.', drop_level=1)
            except Exception as e:
                self.print(f'Failed to get lyrics from {lyrics_module_name}: {e}', drop_level=1)
        
        # 2. Check if current service supports lyrics
        elif ModuleModes.lyrics in self.module_settings[self.service_name].module_supported_modes:
            try:
                kwargs = getattr(track_info, 'lyrics_extra_kwargs', {}) or {}
                lyrics = self.service.get_track_lyrics(track_id, **kwargs)
                if lyrics and (lyrics.embedded or lyrics.synced):
                     self.print('Lyrics downloaded!', drop_level=1)
                     return lyrics
            except Exception as e:
                 self.print(f'Failed to get lyrics from {self.service_name}: {e}', drop_level=1)
        
        return None

    def search_by_tags(self, module_name, track_info: TrackInfo):
        return self.loaded_modules[module_name].search(DownloadTypeEnum.track,
                                                       f'{track_info.name} {" ".join(track_info.artists)}',
                                                       track_info=track_info)

    def _concurrent_download_tracks(self, track_list, download_args_list, concurrent_downloads, performance_summary_indent=0):
        if concurrent_downloads <= 1:
            results = []
            for i, args in enumerate(download_args_list):
                try:
                    res = self.download_track(**args)
                    results.append((i, res, None))
                except Exception as e:
                    results.append((i, None, e))
            return results

        from utils.utils import create_aiohttp_session
        total_tracks = len(track_list)
        results = [None] * total_tracks

        async def download_worker_async(session, index, args):
            track_name = f"Track {args.get('track_id', 'Unknown')}" 
            try:
                track_id = args['track_id']
                loop = asyncio.get_event_loop()
                if await loop.run_in_executor(None, self._check_db, track_id):
                    return (index, f"ID: {track_id}", "SKIPPED", "ALREADY_IN_DB", None)
                
                quality_tier = QualityEnum[self.global_settings['general']['download_quality'].upper()]
                codec_options = CodecOptions(spatial_codecs=self.global_settings['codecs']['spatial_codecs'], proprietary_codecs=self.global_settings['codecs']['proprietary_codecs'])
                
                track_info = await loop.run_in_executor(None, lambda: self.service.get_track_info(track_id, quality_tier, codec_options, **args.get('extra_kwargs', {})))

                if track_info and getattr(track_info, 'error', None):
                    return (index, f"Track {track_id}", "ERROR", track_info.error, None)

                if track_info:
                    track_name = f"{', '.join(track_info.artists)} - {track_info.name}"
                    forced_discs = args.get('forced_total_discs')
                    if forced_discs: track_info.tags.total_discs = forced_discs
                    
                    custom_fmt = args.get('custom_filename_format')
                    extra_data = args.get('extra_template_data')
                    loc = self._create_track_location(args.get('album_location', ''), track_info, custom_format=custom_fmt, extra_template_data=extra_data, forced_total_discs=forced_discs)

                    existing_loc = await loop.run_in_executor(None, self._track_file_exists, loc)
                    if existing_loc:
                        loc = existing_loc
                        lrc_path = os.path.splitext(loc)[0] + '.lrc'
                        if not await loop.run_in_executor(None, os.path.exists, lrc_path):
                             lyrics_info = await loop.run_in_executor(None, self._get_lyrics, track_id, track_info)
                             if lyrics_info and lyrics_info.synced:
                                 try:
                                     self.print(f'Saving missing synced lyrics to {os.path.basename(lrc_path)}...', drop_level=1)
                                     with open(lrc_path, 'w', encoding='utf-8') as f:
                                         f.write(lyrics_info.synced)
                                 except Exception as e:
                                     self.print(f'Failed to save synced lyrics: {e}', drop_level=1)

                        await loop.run_in_executor(None, self._add_to_db, track_info, loc)
                        return (index, track_name, "SKIPPED", None, None)

                # --- LÓGICA DE REINTENTO (Restaurada) ---
                download_info = None
                
                if hasattr(track_info, 'download_extra_kwargs') and track_info.download_extra_kwargs:
                     try:
                         download_info = await loop.run_in_executor(None, lambda: self.service.get_track_download(**track_info.download_extra_kwargs))
                     except Exception:
                         download_info = None

                if not download_info:
                    download_kwargs = args.get('extra_kwargs', {}).copy()
                    if 'data' in download_kwargs: download_kwargs.pop('data')

                    try:
                        download_info = await loop.run_in_executor(None, lambda: self.service.get_track_download(track_id, quality_tier, codec_options=codec_options, **download_kwargs))
                    except TypeError:
                        try:
                            download_info = await loop.run_in_executor(None, lambda: self.service.get_track_download(track_id, quality_tier, **download_kwargs))
                        except TypeError:
                            download_info = await loop.run_in_executor(None, lambda: self.service.get_track_download(track_id, quality_tier))
                
                res = await self._download_track_async(session, track_info=track_info, download_info=download_info, **args, verbose=False)
                
                if isinstance(res, tuple) and res[0]:
                    await loop.run_in_executor(None, self._add_to_db, track_info, res[0])
                    return (index, track_name, None, res[0], None)
                
                if res and isinstance(res, tuple) and res[0] is None and res[1] == "INVALID_URL":
                     return (index, track_name, "ERROR", None, Exception(f"Invalid Stream URL (ID returned): {res[2]}"))
                
                if res and isinstance(res, tuple) and res[0] is None and res[1] == "VIDEO_DISABLED":
                     return (index, track_name, "SKIPPED", None, Exception("Video download disabled in settings"))

                return (index, track_name, "ERROR", None, Exception("Download returned None"))
            except Exception as e:
                error_msg = str(e)
                if error_msg.isdigit():
                    error_msg = f"Track unavailable on server (Content Removed/Blocked). ID: {error_msg}"
                    return (index, track_name, "ERROR", None, Exception(error_msg))
                return (index, track_name, "ERROR", None, e)

        async def run_concurrent():
            async with create_aiohttp_session() as session:
                semaphore = asyncio.Semaphore(concurrent_downloads)
                async def bounded_download(i, a):
                    async with semaphore: return await download_worker_async(session, i, a)
                tasks = [bounded_download(i, a) for i, a in enumerate(download_args_list)]
                symbols = self._get_status_symbols()
                
                # --- FIX: USO CORRECTO DE TQDM.TQDM ---
                iterator = asyncio.as_completed(tasks)
                if self.global_settings["general"].get("progress_bar", False) and tqdm_progress is not None:
                    # Usamos tqdm_progress() explícitamente porque importamos 'import tqdm'
                    iterator = tqdm_progress(iterator, total=total_tracks, unit='tr', leave=False)
                
                completed = 0
                for coro in iterator:
                    index, name, status, dl_res, error = await coro
                    completed += 1
                    
                    if status == "ERROR":
                        status_str = symbols['error']
                        error_msg = str(error) if error else "Unknown error"
                        msg = f"{completed}/{total_tracks} {status_str} {name}: {symbols['red_text']}{error_msg}{symbols['reset']}"
                        if self.global_settings["general"].get("progress_bar", False) and tqdm_progress is not None:
                            tqdm_progress.write(msg)
                        else:
                            self.print(msg, drop_level=performance_summary_indent)
                            
                    elif status == "SKIPPED":
                        msg_suffix = "(already exists)"
                        if error and str(error) == "Video download disabled in settings":
                             msg_suffix = "(video disabled)"
                        
                        msg = f"{completed}/{total_tracks} {symbols['skip']} {name} {symbols['yellow_text']}{msg_suffix}{symbols['reset']}"
                        if self.global_settings["general"].get("progress_bar", False) and tqdm_progress is not None:
                            tqdm_progress.write(msg)
                        else:
                            self.print(msg, drop_level=performance_summary_indent)
                    else:
                        msg = f"{completed}/{total_tracks} {symbols['success']} {name}"
                        if self.global_settings["general"].get("progress_bar", False) and tqdm_progress is not None:
                            tqdm_progress.write(msg)
                        else:
                            self.print(msg, drop_level=performance_summary_indent)
                        
                    results[index] = (index, dl_res, error)
            return results

        return asyncio.run(run_concurrent())

    @staticmethod
    def _get_artist_initials_from_name(name: str) -> str:
        if not name: return '#'
        ch = name.strip()[0].upper()
        decomposed = unicodedata.normalize('NFD', ch)
        base = ''.join(c for c in decomposed if unicodedata.category(c) != 'Mn').upper()
        return base if ('A' <= base <= 'Z') else '#'

    def _create_track_location(self, album_location: str, track_info: TrackInfo, custom_format: str = None, extra_template_data: dict = None, forced_total_discs: int = None) -> str:
        data = prepare_template_data(track_info=track_info, extra_tags=extra_template_data)
        
        # --- VIDEO PATH LOGIC ---
        is_video = track_info.codec in [CodecEnum.H264, CodecEnum.H265]
        video_path_setting = self.global_settings['general'].get('video_download_path')
        
        video_format_string = None
        if is_video and video_path_setting:
            if '{' in video_path_setting:
                video_format_string = video_path_setting
                base_path = ""
            else:
                base_path = video_path_setting
            # Treat as single to use single_full_path_format (or video_format_string)
            album_location = None
        else:
            base_path = album_location if album_location else self.path
        # ------------------------
        
        # --- ZFILL LOGIC ---
        if self.global_settings['formatting'].get('enable_zfill', False):
            try:
                total = int(data.get('total_tracks', 0))
                pad_len = len(str(total)) if total > 9 else 2
                
                if 'track_number' in data and data['track_number']:
                    data['track_number'] = str(data['track_number']).zfill(pad_len)
                if 'disc_number' in data and data['disc_number']:
                    data['disc_number'] = str(data['disc_number']).zfill(2)
            except Exception:
                pass
        # -------------------
        
        if custom_format:
            format_string = custom_format
        elif video_format_string:
            format_string = video_format_string
        elif (not album_location) or (album_location == self.path):
            format_string = self.global_settings['formatting']['single_full_path_format']
        else:
            format_string = self.global_settings['formatting']['track_filename_format']
        
        temp_artist = data.get('artist', 'Unknown')
        data['artist_initials'] = self._get_artist_initials_from_name(temp_artist)

        # Use forced_total_discs if provided, otherwise use metadata
        total_discs = forced_total_discs if forced_total_discs is not None else int(data.get('total_discs') or 0)
        disc_num = int(data.get('disc_number') or 1)
        
        # FALLBACK: If disc_number > 1 but total_discs <= 1, assume multi-disc album
        if disc_num > 1 and total_discs <= 1:
            total_discs = disc_num  # At least disc_num discs exist
        
        for pat in ['Disc {disc_number}/', 'Disk {disc_number}/', 'CD {disc_number}/']:
            format_string = format_string.replace(pat, '')
        format_string = format_string.lstrip('/')
        
        # Create Disc folder if this is a multi-disc album
        if total_discs > 1 and not custom_format:
            format_string = f"Disc {disc_num}/{format_string}"
            
        filename_rel = format_template_advanced(format_string, data)
        ext = {
            CodecEnum.FLAC: '.flac',
            CodecEnum.MP3: '.mp3',
            CodecEnum.AAC: '.m4a',
            CodecEnum.ALAC: '.m4a',
            CodecEnum.H264: '.mp4',
            CodecEnum.H265: '.mp4'
        }.get(track_info.codec, '.flac')
        if not filename_rel.lower().endswith(ext): filename_rel += ext
        
        if platform.system() == 'Windows' and _DRIVE_RE.match(filename_rel.split('/')[0]):
            track_location = os.path.abspath(filename_rel)
        else:
            track_location = os.path.join(base_path, filename_rel)

        if platform.system() == 'Windows':
            track_location = os.path.abspath(track_location)
            if not track_location.startswith('\\\\?\\'): track_location = '\\\\?\\' + track_location

        # Fuzzy folder match: if a sibling folder exists with same normalized name, use it
        # This prevents duplicate folders differing only in diacritics
        track_location = self._resolve_fuzzy_folder(track_location)

        return track_location

    @staticmethod
    def _find_fuzzy_folder(parent_dir: str, folder_name: str) -> str:
        """Return an existing subfolder of parent_dir whose name matches folder_name
        when diacritics are ignored. Returns folder_name unchanged if none found."""
        norm = _normalize_for_compare(folder_name)
        try:
            if os.path.isdir(parent_dir):
                for entry in os.listdir(parent_dir):
                    if entry != folder_name and os.path.isdir(os.path.join(parent_dir, entry)):
                        if _normalize_for_compare(entry) == norm:
                            return entry
        except Exception:
            pass
        return folder_name

    def _resolve_fuzzy_folder(self, loc: str, is_dir: bool = False) -> str:
        """Rewrite loc so that every folder component uses any existing
        fuzzy-matching folder name instead of creating a new one.
        Set is_dir=True when loc is a directory path (all parts are folders)."""
        prefix = '\\\\?\\' if loc.startswith('\\\\?\\') else ''
        clean_loc = loc[len(prefix):]

        parts = [p for p in clean_loc.replace('\\', '/').split('/') if p]
        if not parts:
            return loc

        # Build resolved path — handle Windows drive letter (e.g. 'Z:')
        if _DRIVE_RE.match(parts[0]):
            resolved = parts[0] + '\\'
            parts = parts[1:]
        else:
            resolved = os.sep

        if is_dir:
            # All parts are folders — fuzzy-match every component
            for part in parts:
                actual = self._find_fuzzy_folder(resolved, part)
                resolved = os.path.join(resolved, actual)
        else:
            # Last part is the filename — match all folder parts, keep filename
            for part in parts[:-1]:
                actual = self._find_fuzzy_folder(resolved, part)
                resolved = os.path.join(resolved, actual)
            resolved = os.path.join(resolved, parts[-1]) if parts else resolved

        return prefix + resolved

    def _create_album_location(self, path: str, album_id: str, album_info: AlbumInfo) -> str:
        if album_info.release_date and len(album_info.release_date) >= 10:
            full_release_date = album_info.release_date[:10]
        else:
            full_release_date = str(album_info.release_year) if album_info.release_year else ""

        release_type = determine_release_type(album_info=album_info)
        data = {
            'artist': album_info.artist,
            'album_artist': album_info.artist,
            'main_artist': album_info.artist,
            'name': album_info.name,
            'album': album_info.name,
            'album_clean': clean_string_logic(album_info.name),
            'release_date': full_release_date,
            'year': str(full_release_date)[:4] if len(str(full_release_date)) >= 4 else "",
            'release': release_type,
            'explicit': Explicit(getattr(album_info, 'explicit', False)),
            'artist_initials': self._get_artist_initials_from_name(album_info.artist)
        }

        format_string = self.global_settings['formatting']['album_format']
        format_string = format_string.replace('[{release}]', '{release}')
        format_string = format_string.replace('[{quality}]', '{quality}')
        format_string = format_string.replace('[{explicit}]', '{explicit}')
        try:
            album_path_formatted_name = format_template_advanced(format_string, data)
        except Exception as e:
            logging.error(f"Error formatting album path: {e}")
            relative_path = f"{sanitise_name(data['artist'])}/{sanitise_name(data['name'])}"
            album_path_formatted_name = relative_path

        album_path = os.path.join(path, album_path_formatted_name)
        try:
            album_path = fix_byte_limit(album_path) + '/'
        except ValueError:
            album_path = album_path + '/'

        album_path = self._resolve_fuzzy_folder(album_path.rstrip('/'), is_dir=True) + '/'
        os.makedirs(album_path, exist_ok=True)
        return album_path

    def _create_album_location_from_track(self, track_info: TrackInfo) -> str:
        t_tags = getattr(track_info, 'tags', None)
        album_name = 'Unknown Album'
        album_artist = 'Unknown Artist'
        release_date = ''
        if t_tags:
            album_name = getattr(t_tags, 'album', '') or getattr(t_tags, 'album_name', '') or 'Unknown Album'
            album_artist = getattr(t_tags, 'album_artist', '')
            release_date = getattr(t_tags, 'release_date', '')
            if release_date and len(release_date) > 10:
                release_date = release_date[:10]
        if not album_artist and track_info.artists:
            album_artist = str(track_info.artists[0]) if isinstance(track_info.artists, list) else str(
                track_info.artists)

        release_type = determine_release_type(track_info=track_info)
        data = {
            'artist': album_artist,
            'main_artist': album_artist,
            'name': album_name,
            'album': album_name,
            'album_clean': clean_string_logic(album_name),
            'release_date': release_date,
            'year': release_date[:4] if len(release_date) >= 4 else "",
            'release': release_type,
            'explicit': Explicit(getattr(track_info, 'explicit', False)),
            'artist_initials': self._get_artist_initials_from_name(album_artist)
        }
        format_string = self.global_settings['formatting']['album_format']
        format_string = format_string.replace('[{release}]', '{release}')
        format_string = format_string.replace('[{quality}]', '{quality}')
        format_string = format_string.replace('[{explicit}]', '{explicit}')
        try:
            relative_path = format_template_advanced(format_string, data)
        except Exception as e:
            relative_path = f"{sanitise_name(album_artist)}/{sanitise_name(album_name)}"
        album_path = os.path.join(self.path, relative_path)
        try:
            album_path = fix_byte_limit(album_path) + '/'
        except ValueError:
            album_path = album_path + '/'
        album_path = self._resolve_fuzzy_folder(album_path.rstrip('/'), is_dir=True) + '/'
        os.makedirs(album_path, exist_ok=True)
        return album_path

    def download_album(self, album_id, artist_name='', path=None, indent_level=1, extra_kwargs=None):
        self.set_indent_number(indent_level)
        self.print(f'Fetching album data...')
        album_info: AlbumInfo = self.service.get_album_info(album_id, **(extra_kwargs or {}))
        if not album_info: return []
        
        # --- PRINTS RESTAURADOS ---
        self.print(f'=== Downloading album {album_info.name} ({album_info.id}) ===', drop_level=1)
        self.print(f'Artist: {album_info.artist} ({album_info.artist_id})')
        if album_info.release_year: self.print(f'Year: {album_info.release_year}')
        if album_info.duration: self.print(f'Duration: {beauty_format_seconds(album_info.duration)}')
        number_of_tracks = len(album_info.tracks)
        self.print(f'Number of tracks: {number_of_tracks!s}')
        colored_platform = get_colored_platform_name(self.module_settings[self.service_name].service_name)
        self.print(f'Platform: {colored_platform}')
        # BETTER DETECTION: Get track info for first few tracks to detect discs
        detected_total_discs = 1
        try:
            if album_info.tracks and len(album_info.tracks) > 0:
                # Sample up to first 5 tracks to detect disc structure
                quality_tier = QualityEnum[self.global_settings['general']['download_quality'].upper()]
                codec_options = CodecOptions(
                    spatial_codecs=self.global_settings['codecs']['spatial_codecs'],
                    proprietary_codecs=self.global_settings['codecs']['proprietary_codecs']
                )
                # Smart sampling: first 3 and last 2 tracks to catch multi-disc
                sample_indices = []
                if len(album_info.tracks) <= 5:
                    sample_indices = list(range(len(album_info.tracks)))
                else:
                    # Sample first 3 and last 2
                    sample_indices = [0, 1, 2, len(album_info.tracks) - 2, len(album_info.tracks) - 1]
                
                disc_numbers = []
                for i in sample_indices:
                    track_item = album_info.tracks[i]
                    track_id = track_item.id if hasattr(track_item, 'id') else track_item
                    try:
                        track_info_sample = self.service.get_track_info(track_id, quality_tier, codec_options, **(album_info.track_extra_kwargs or {}))
                        if track_info_sample and hasattr(track_info_sample, 'tags') and track_info_sample.tags:
                            disc_num = getattr(track_info_sample.tags, 'disc_number', 1)
                            total_disc = getattr(track_info_sample.tags, 'total_discs', 0)
                            if disc_num: disc_numbers.append(disc_num)
                            if total_disc > detected_total_discs:
                                detected_total_discs = total_disc
                    except:
                        pass
                
                # If we found any disc numbers, use the max
                if disc_numbers:
                    max_disc_from_sample = max(disc_numbers)
                    detected_total_discs = max(detected_total_discs, max_disc_from_sample)
        except Exception as e:
            logging.debug(f"Could not detect disc structure: {e}")
        
        album_path = self._create_album_location(path if path else self.path, album_id, album_info)
        
        concurrent_downloads = self.global_settings['general'].get('concurrent_downloads', 1)
        download_args_list = []
        for index, track_item in enumerate(album_info.tracks, start=1):
            t_id = track_item.id if hasattr(track_item, 'id') else track_item
            download_args_list.append({
                'track_id': t_id, 'album_location': album_path, 'track_index': index,
                'number_of_tracks': number_of_tracks, 'indent_level': 1,
                'extra_kwargs': album_info.track_extra_kwargs,
                'forced_total_discs': detected_total_discs
            })
        # Pre-flight: if album comes from a non-primary storefront, test availability
        # via a lightweight webplayback check to avoid 18x "unavailable" errors
        album_storefront = (album_info.track_extra_kwargs or {}).get('storefront', 'us')
        primary_sf = getattr(self.service, '_primary_storefront', 'us')
        if album_storefront.lower() not in ('us', primary_sf.lower()):
            first_id = album_info.tracks[0] if album_info.tracks else None
            if first_id:
                try:
                    available = self.service.check_track_available(str(first_id))
                    if not available:
                        self.print(f'Album not streamable in your region (storefront: {album_storefront}). Skipping.', drop_level=1)
                        return []
                except Exception:
                    pass  # If check fails, try anyway

        self._concurrent_download_tracks(album_info.tracks, download_args_list, concurrent_downloads, performance_summary_indent=0)
        return album_info.tracks

    def download_artist(self, artist_id, extra_kwargs=None):
        self.set_indent_number(1)
        
        fetch_credited_albums = False
        if 'artist_downloading' in self.global_settings and 'return_credited_albums' in self.global_settings['artist_downloading']:
            fetch_credited_albums = self.global_settings['artist_downloading']['return_credited_albums']

        artist_info: ArtistInfo = self.service.get_artist_info(artist_id, fetch_credited_albums, **(extra_kwargs or {}))
        
        if not artist_info: 
            self.print("Failed to retrieve artist info.")
            return

        initial = self._get_artist_initials_from_name(artist_info.name)
        
        # --- FIX: CARPETAS DUPLICADAS EN ARTISTA ---
        # NO construimos una ruta manual aquí (artist_path).
        # Usamos self.path (la raíz) y dejamos que download_album construya la estructura
        # basada en 'album_format' de settings.json ({artist_initials}/{artist}/...)
        
        # PERO: Calculamos artist_path solo para los tracks sueltos más abajo
        artist_path = os.path.join(self.path, initial, sanitise_name(artist_info.name)) + '/'
        
        self.print(f'=== Artist: {artist_info.name} ===')
        for index, album_item in enumerate(artist_info.albums, start=1):
            self.print(f'Album {index}/{len(artist_info.albums)}')
            album_id = album_item.id if hasattr(album_item, 'id') else str(album_item)
            
            # Pasamos self.path en lugar de artist_path para evitar duplicidad en ALBUMES
            self.download_album(album_id, artist_name=artist_info.name, path=self.path, indent_level=2, extra_kwargs=artist_info.album_extra_kwargs)

        # --- TRACKS SUELTOS (SINGLES) ---
        # Aquí sí usamos artist_path para que queden en la carpeta del artista y no en la raíz
        self.set_indent_number(2)
        skip_tracks = self.global_settings['artist_downloading']['separate_tracks_skip_downloaded']
        # NOTA: En versiones antiguas del script, artist_info.tracks podía contener tracks sueltos.
        # Si existen, los procesamos también pasando self.path para que usen la estructura correcta.
        if artist_info.tracks:
             # Aquí podríamos implementar la descarga de tracks sueltos si fuera necesario
             pass

    async def _download_track_async(self, session, track_id=None, track_info=None, download_info=None, album_location='', custom_filename_format=None, extra_template_data=None, **kwargs):
        from utils.utils import download_file_async
        if not track_info or not download_info: return None
        
        # --- VIDEO SWITCH LOGIC ---
        is_video = track_info.codec in [CodecEnum.H264, CodecEnum.H265]
        download_videos_enabled = self.global_settings['general'].get('download_videos', True)
        
        if is_video and not download_videos_enabled:
             # Devolvemos un estado "SKIPPED" pero indicando que fue por configuración
             # Como esta función retorna (final_loc, bytes_downloaded) o None,
             # retornar None aquí hará que _concurrent_download_tracks lo trate como error genérico.
             # Para manejarlo bien, podríamos lanzar una excepción controlada o simplemente return None
             # y dejar que el llamador lo ignore.
             # Sin embargo, la mejor forma de integrarlo es lanzar una excepción específica
             # para que el reporte final diga "Video disabled".
             return None, "VIDEO_DISABLED"
        # --------------------------

        forced_discs = kwargs.get('forced_total_discs')
        loc = self._create_track_location(album_location, track_info, custom_format=custom_filename_format, extra_template_data=extra_template_data, forced_total_discs=forced_discs)
        os.makedirs(os.path.dirname(loc), exist_ok=True)
        if download_info.download_type is DownloadEnum.URL:
            final_loc, _ = await download_file_async(session, download_info.file_url, loc, headers=download_info.file_url_headers)
        else:
            final_loc = shutil.move(download_info.temp_file_path, loc)
        
        if final_loc:
            # Artwork
            artwork_path = ''
            if track_info.cover_url and self.global_settings['covers']['embed_cover']:
                try:
                    artwork_path = self.create_temp_filename()
                    art_res = await download_file_async(session, track_info.cover_url, artwork_path, 
                                                      artwork_settings=self._get_artwork_settings(), enable_progress_bar=False)
                    if isinstance(art_res, tuple): artwork_path = art_res[0]
                    else: artwork_path = art_res
                except: artwork_path = ''

            # Convert
            loop = asyncio.get_event_loop()
            conversion_result = await loop.run_in_executor(None, self._convert_file_if_needed, final_loc, track_info, lambda x: None)
            converted_loc, old_loc, old_container = conversion_result
            if converted_loc and converted_loc != final_loc:
                 final_loc = converted_loc

            # Detect Container
            file_extension = os.path.splitext(final_loc)[1].lower()
            container_map = {
                '.flac': ContainerEnum.flac, '.mp3': ContainerEnum.mp3,
                '.m4a': ContainerEnum.m4a, '.opus': ContainerEnum.opus,
                '.ogg': ContainerEnum.ogg, '.wav': ContainerEnum.wav,
                '.aiff': ContainerEnum.aiff, '.mp4': ContainerEnum.mp4
            }
            container = container_map.get(file_extension, ContainerEnum.flac)
            
            # Tag
            # Calculate LRC path before getting lyrics to check if it exists
            lrc_path = os.path.splitext(final_loc)[0] + '.lrc'
            lrc_exists = os.path.exists(lrc_path)
            
            if lrc_exists:
                 self.print(f'LRC file already exists: {os.path.basename(lrc_path)}. Skipping lyric download.', drop_level=1)
                 lyrics_info = None
            else:
                 lyrics_info = self._get_lyrics(track_id, track_info)
            
            lyrics = (lyrics_info.embedded if lyrics_info else None) or ""
            
            # Save synced lyrics to .lrc file
            if lyrics_info and lyrics_info.synced and not lrc_exists:
                try:
                    self.print(f'Saving synced lyrics to {os.path.basename(lrc_path)}...', drop_level=1)
                    with open(lrc_path, 'w', encoding='utf-8') as f:
                        f.write(lyrics_info.synced)
                except Exception as e:
                    self.print(f'Failed to save synced lyrics: {e}', drop_level=1)
            elif lyrics_info and lyrics_info.embedded:
                 self.print('Only unsynced lyrics available. Embedding in file...', drop_level=1)

            if not is_video:
                if lyrics:
                    self.print('Embedding lyrics into audio file...', drop_level=1)
                await loop.run_in_executor(None, tag_file, final_loc, artwork_path if os.path.exists(artwork_path) else None, track_info, [], lyrics, container)

            if artwork_path and os.path.exists(artwork_path): os.remove(artwork_path)
            return (final_loc, 0)
        return None

    def download_track(self, track_id, album_location='', track_index=0, number_of_tracks=0, indent_level=1, extra_kwargs={}, forced_total_discs=None, custom_filename_format=None, extra_template_data=None, verbose=True, m3u_playlist=None, **_):
        self.set_indent_number(indent_level)
        if self._check_db(track_id): return "SKIPPED"
        quality_tier = QualityEnum[self.global_settings['general']['download_quality'].upper()]
        codec_options = CodecOptions(spatial_codecs=self.global_settings['codecs']['spatial_codecs'], proprietary_codecs=self.global_settings['codecs']['proprietary_codecs'])
        track_info = self.service.get_track_info(track_id, quality_tier, codec_options, **extra_kwargs)
        if forced_total_discs: track_info.tags.total_discs = forced_total_discs
        is_video = track_info.codec in [CodecEnum.H264, CodecEnum.H265]
        if getattr(track_info, 'error', None):
            self.print(f'Error getting track info: {track_info.error}', drop_level=1)
            return None
        loc = self._create_track_location(album_location, track_info, custom_format=custom_filename_format, extra_template_data=extra_template_data, forced_total_discs=forced_total_discs)
        existing_loc = self._track_file_exists(loc)
        if existing_loc:
            loc = existing_loc
            lrc_path = os.path.splitext(loc)[0] + '.lrc'
            
            if not os.path.exists(lrc_path):
                 self.print(f'Track exists but LRC missing. Checking for lyrics...', drop_level=1)
                 lyrics_info = self._get_lyrics(track_id, track_info)
                 if lyrics_info and lyrics_info.synced:
                     try:
                        self.print(f'Saving synced lyrics to {os.path.basename(lrc_path)}...', drop_level=1)
                        with open(lrc_path, 'w', encoding='utf-8') as f:
                            f.write(lyrics_info.synced)
                     except Exception as e:
                        self.print(f'Failed to save synced lyrics: {e}', drop_level=1)
            
            self._add_to_db(track_info, loc)
            return "SKIPPED"
        
        # --- LÓGICA DE REINTENTO (Sincrona) ---
        download_info = None
        if hasattr(track_info, 'download_extra_kwargs') and track_info.download_extra_kwargs:
             try:
                 download_info = self.service.get_track_download(**track_info.download_extra_kwargs)
             except Exception:
                 download_info = None

        if not download_info:
            download_kwargs = extra_kwargs.copy()
            if 'data' in download_kwargs: download_kwargs.pop('data')

            try:
                download_info = self.service.get_track_download(track_id, quality_tier, codec_options=codec_options, **download_kwargs)
            except TypeError:
                try:
                    download_info = self.service.get_track_download(track_id, quality_tier, **download_kwargs)
                except TypeError:
                    download_info = self.service.get_track_download(track_id, quality_tier)
        # -----------------------------------------------

        os.makedirs(os.path.dirname(loc), exist_ok=True)
        final_loc = download_file(download_info.file_url, loc, headers=download_info.file_url_headers) if download_info.download_type is DownloadEnum.URL else shutil.move(download_info.temp_file_path, loc)
        
        if final_loc:
            # Artwork
            artwork_path = ''
            if track_info.cover_url and self.global_settings['covers']['embed_cover']:
                artwork_path = self.create_temp_filename()
                download_file(track_info.cover_url, artwork_path, artwork_settings=self._get_artwork_settings(), enable_progress_bar=False)

            # Convert
            conversion_result = self._convert_file_if_needed(final_loc, track_info, lambda x: None)
            converted_loc, old_loc, old_container = conversion_result
            if converted_loc and converted_loc != final_loc:
                 final_loc = converted_loc

            # Detect Container
            file_extension = os.path.splitext(final_loc)[1].lower()
            container_map = {
                '.flac': ContainerEnum.flac, '.mp3': ContainerEnum.mp3,
                '.m4a': ContainerEnum.m4a, '.opus': ContainerEnum.opus,
                '.ogg': ContainerEnum.ogg, '.wav': ContainerEnum.wav,
                '.aiff': ContainerEnum.aiff
            }
            container = container_map.get(file_extension, ContainerEnum.flac)
            # Tag
            # Calculate LRC path before getting lyrics to check if it exists
            lrc_path = os.path.splitext(final_loc)[0] + '.lrc'
            lrc_exists = os.path.exists(lrc_path)
            
            if lrc_exists:
                 self.print(f'LRC file already exists: {os.path.basename(lrc_path)}. Skipping lyric download.', drop_level=1)
                 lyrics_info = None
            else:
                 lyrics_info = self._get_lyrics(track_id, track_info)

            lyrics = ""
            if lyrics_info:
                lyrics = lyrics_info.synced or lyrics_info.embedded or ""
            
            # Save synced lyrics to .lrc file
            if lyrics_info and lyrics_info.synced and not lrc_exists:
                try:
                    self.print(f'Saving synced lyrics to {os.path.basename(lrc_path)}...', drop_level=1)
                    with open(lrc_path, 'w', encoding='utf-8') as f:
                        f.write(lyrics_info.synced)
                except Exception as e:
                    self.print(f'Failed to save synced lyrics: {e}', drop_level=1)
            elif lyrics_info and lyrics_info.embedded:
                 self.print('Only unsynced lyrics available. Embedding in file...', drop_level=1)

            if not is_video:
                if lyrics:
                    self.print('Embedding lyrics into audio file...', drop_level=1)
                tag_file(final_loc, artwork_path if os.path.exists(artwork_path) else None, track_info, [], lyrics, container)

            if artwork_path and os.path.exists(artwork_path): os.remove(artwork_path)
            self._add_to_db(track_info, final_loc)
            return final_loc
        return None

    def download_playlist(self, playlist_id, custom_module=None, extra_kwargs=None):
        import time
        self.set_indent_number(1)
        service_name_lower = ""
        if hasattr(self, 'service_name') and self.service_name:
            service_name_lower = self.service_name.lower()
        kwargs_for_playlist_info = {}
        if extra_kwargs: kwargs_for_playlist_info.update(extra_kwargs)
        if service_name_lower in ['beatport', 'beatsource']:
            if 'data' in kwargs_for_playlist_info:
                kwargs_for_playlist_info.pop('data', None)

        playlist_info: PlaylistInfo = self.service.get_playlist_info(playlist_id, **kwargs_for_playlist_info)

        self.print(f'=== Downloading playlist {playlist_info.name} ({playlist_id}) ===', drop_level=1)
        self.print(f'Playlist creator: {playlist_info.creator}' + (
            f' ({playlist_info.creator_id})' if playlist_info.creator_id else ''))
        if playlist_info.release_year: self.print(f'Playlist creation year: {playlist_info.release_year}')
        if playlist_info.duration: self.print(f'Duration: {beauty_format_seconds(playlist_info.duration)}')
        number_of_tracks = len(playlist_info.tracks)
        self.print(f'Number of tracks: {number_of_tracks!s}')
        safe_playlist_name = sanitise_name(playlist_info.name)
        if len(safe_playlist_name) > 50: safe_playlist_name = safe_playlist_name[:50]

        # --- "HACK" PARA USAR CONFIGURACIÓN DE PLAYLISTS DESDE SETTINGS.JSON ---
        # Leemos el campo 'playlist_format' que no se borra.
        # Si tiene '|||', es que el usuario quiere separar Carpeta ||| Archivo.
        
        raw_playlist_format = self.global_settings['formatting']['playlist_format']
        PLAYLIST_TRACK_STRUCTURE_LOCAL = PLAYLIST_TRACK_STRUCTURE # Usar variable global por defecto
        use_default_playlist_folder = True

        if "|||" in raw_playlist_format:
            # El usuario configuró: "E:/{name} ||| {artists} - {title}..."
            parts = raw_playlist_format.split("|||")
            if len(parts) >= 2:
                folder_format = parts[0].strip()
                file_format = parts[1].strip()
                
                # Combinamos para crear la ruta completa del archivo
                PLAYLIST_TRACK_STRUCTURE_LOCAL = f"{folder_format}/{file_format}"
                
                # Si es una ruta absoluta en Windows (E:/...), desactivamos la carpeta por defecto del programa
                if platform.system() == 'Windows' and _DRIVE_RE.match(folder_format.split('/')[0]):
                    use_default_playlist_folder = False
                
                # Sobrescribimos temporalmente el formato de playlist para que use solo la parte de la carpeta
                # (para crear M3U y portada en el lugar correcto)
                self.global_settings['formatting']['playlist_format'] = folder_format
        
        elif PLAYLIST_TRACK_STRUCTURE_LOCAL and platform.system() == 'Windows' and _DRIVE_RE.match(PLAYLIST_TRACK_STRUCTURE_LOCAL.split('/')[0]):
             # Si no hay ||| pero hay una variable global con ruta absoluta, también desactivamos carpeta default
             use_default_playlist_folder = False
        # ------------------------------------------------------------------------

        extra_template_data = {'name': safe_playlist_name, 'playlist_name': safe_playlist_name}

        m3u_playlist_path = None
        
        if use_default_playlist_folder or (PLAYLIST_TRACK_STRUCTURE_LOCAL and not use_default_playlist_folder):
            # Calculamos la ruta de la carpeta de la playlist (sea por defecto o personalizada E:/)
            playlist_tags = {k: sanitise_name(v) for k, v in asdict(playlist_info).items()}
            playlist_tags['name'] = safe_playlist_name
            playlist_tags['explicit'] = Explicit(playlist_info.explicit)

            format_string = self.global_settings['formatting']['playlist_format']
            format_string = format_string.replace('[{explicit}]', '{explicit}')
            playlist_path_formatted_name = format_string.format(**playlist_tags)

            if use_default_playlist_folder:
                playlist_path = os.path.join(self.path, playlist_path_formatted_name)
            else:
                # Si es ruta absoluta personalizada
                playlist_path = playlist_path_formatted_name

            playlist_path = fix_byte_limit(playlist_path) + '/'
            os.makedirs(playlist_path, exist_ok=True)

            # Playlist cover download disabled
            # if playlist_info.cover_url:
            #     self.print('Downloading playlist cover')
            #     download_file(playlist_info.cover_url, f'{playlist_path}cover.{playlist_info.cover_type.name}',
            #                   artwork_settings=self._get_artwork_settings())

            if playlist_info.description:
                with open(playlist_path + 'description.txt', 'w', encoding='utf-8') as f: f.write(playlist_info.description)

            if self.global_settings['playlist']['save_m3u']:
                m3u_playlist_path = os.path.join(playlist_path, f'{safe_playlist_name}.m3u')
                with open(m3u_playlist_path, 'w', encoding='utf-8') as f: f.write('')
                if self.global_settings['playlist']['extended_m3u']:
                    with open(m3u_playlist_path, 'a', encoding='utf-8') as f: f.write('#EXTM3U\n\n')
        else:
            playlist_path = "" 

        colored_platform = get_colored_platform_name(self.module_settings[self.service_name].service_name)
        self.print(f'Platform: {colored_platform}')

        concurrent_downloads = self.global_settings['general'].get('concurrent_downloads', 1)
        if service_name_lower == 'spotify': concurrent_downloads = 1
        elif service_name_lower == 'applemusic': concurrent_downloads = 1

        download_args_list = []
        for index, track_id_or_info in enumerate(playlist_info.tracks, start=1):
            actual_track_id_str_for_download = track_id_or_info.id if isinstance(track_id_or_info, TrackInfo) else str(track_id_or_info)
            download_args = {
                'track_id': actual_track_id_str_for_download, 
                'album_location': playlist_path if use_default_playlist_folder else '', 
                'track_index': index, 
                'number_of_tracks': number_of_tracks, 
                'indent_level': 1,
                'm3u_playlist': m3u_playlist_path,
                'extra_kwargs': playlist_info.track_extra_kwargs,
                'custom_filename_format': PLAYLIST_TRACK_STRUCTURE_LOCAL,
                'extra_template_data': extra_template_data
            }
            download_args_list.append(download_args)

        results = self._concurrent_download_tracks(playlist_info.tracks, download_args_list,
                                                   concurrent_downloads, performance_summary_indent=0)
        
        rate_limited_tracks = []
        for index, (original_index, result, error) in enumerate(results):
            if error and result == "RATE_LIMITED":
                actual_track_id_str_for_download = download_args_list[original_index]['track_id']
                rate_limited_tracks.append(
                    {'id': actual_track_id_str_for_download, 'extra_kwargs': playlist_info.track_extra_kwargs,
                     'original_index': original_index + 1})
            elif result == "RATE_LIMITED":
                actual_track_id_str_for_download = download_args_list[original_index]['track_id']
                rate_limited_tracks.append(
                    {'id': actual_track_id_str_for_download, 'extra_kwargs': playlist_info.track_extra_kwargs,
                     'original_index': original_index + 1})

        if rate_limited_tracks:
            self.set_indent_number(1)
            print()
            self.print(f"--- Retrying {len(rate_limited_tracks)} rate-limited tracks ---", drop_level=1)
            for i, retry_item in enumerate(rate_limited_tracks):
                self.set_indent_number(2)
                print()
                self.print(f'Track {retry_item["original_index"]}/{number_of_tracks} (Retry Pass)', drop_level=1)
                self.download_track(retry_item['id'], album_location=playlist_path if use_default_playlist_folder else '',
                                    track_index=retry_item["original_index"], number_of_tracks=number_of_tracks,
                                    indent_level=1, m3u_playlist=m3u_playlist_path,
                                    extra_kwargs=retry_item['extra_kwargs'],
                                    custom_filename_format=PLAYLIST_TRACK_STRUCTURE_LOCAL,
                                    extra_template_data=extra_template_data)
                if i < len(rate_limited_tracks) - 1:
                    print()
                    self.print("Pausing 30 seconds to prevent rate limiting...", drop_level=1)
                    time.sleep(30)

        self.set_indent_number(1)
        symbols = self._get_status_symbols()
        self.print(f'=== {symbols["success"]} Playlist completed ===', drop_level=1)
        print()
        print()

    def _convert_file_if_needed(self, file_path, track_info, d_print):
        """Convert file based on codec_conversions settings"""
        try:
            try:
                from utils.models import CodecEnum, codec_data
                from utils.utils import silentremove
                conversions = {CodecEnum[k.upper()]: CodecEnum[v.upper()] for k, v in
                               self.global_settings['advanced']['codec_conversions'].items()}
            except:
                conversions = {}
                return (file_path, None, None)

            if not conversions: return (file_path, None, None)
            codec = track_info.codec
            if codec not in conversions: return (file_path, None, None)
            new_codec = conversions[codec]
            if codec == new_codec: return (file_path, None, None)

            new_codec_data = codec_data[new_codec]
            temp_track_location = f'{self.create_temp_filename()}.{new_codec_data.container.name}'
            file_path_without_ext = os.path.splitext(file_path)[0]
            new_track_location = f'{file_path_without_ext}.{new_codec_data.container.name}'

            import ffmpeg
            stream = ffmpeg.input(file_path, hide_banner=None, y=None)
            stream.output(temp_track_location, acodec=new_codec.name.lower(), vn=None, loglevel='error').run(capture_stdout=True, capture_stderr=True)

            keep_original = self.global_settings.get('advanced', {}).get('conversion_keep_original', False)
            old_track_location = file_path if keep_original else None
            old_container = codec_data[codec].container if keep_original else None

            shutil.move(temp_track_location, new_track_location)
            if not keep_original: silentremove(file_path)

            return (new_track_location, old_track_location, old_container)

        except Exception as e:
            return (file_path, None, None)

    def _get_artwork_settings(self, module_name=None, is_external=False):
        if not module_name:
            module_name = self.service_name
        return {
            'should_resize': ModuleFlags.needs_cover_resize in self.module_settings[module_name].flags,
            'resolution': self.global_settings['covers']['external_resolution'] if is_external else
            self.global_settings['covers']['main_resolution'],
            'compression': self.global_settings['covers']['external_compression'] if is_external else
            self.global_settings['covers']['main_compression'],
            'format': self.global_settings['covers']['external_format'] if is_external else 'jpg'
        }
