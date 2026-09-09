import base64
import json
import logging
import os
import re
import sys
import concurrent.futures
import threading

# Lazy import ffmpeg to avoid circular import issues in PyInstaller bundles
_ffmpeg_module = None

def _get_ffmpeg():
    """Lazily import ffmpeg module."""
    global _ffmpeg_module
    if _ffmpeg_module is None:
        import ffmpeg
        _ffmpeg_module = ffmpeg
    return _ffmpeg_module

def _get_ffmpeg_cmd():
    """Return path to ffmpeg executable: cwd and script dir first (so root ffmpeg.exe is used), then PATH."""
    import sys
    ffmpeg_name = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    # 1) Current working directory (user ran e.g. "python orpheus.py" from project root)
    cwd_candidate = os.path.join(os.getcwd(), ffmpeg_name)
    if os.path.isfile(cwd_candidate):
        res = os.path.abspath(cwd_candidate)
        logging.debug(f'TIDAL: Using ffmpeg from cwd: {res}')
        return res
    # 2) Directory of the main script (orpheus.py or gui.py)
    try:
        main = sys.modules.get('__main__')
        if main and getattr(main, '__file__', None):
            root = os.path.dirname(os.path.abspath(main.__file__))
            candidate = os.path.join(root, ffmpeg_name)
            if os.path.isfile(candidate):
                res = os.path.abspath(candidate)
                logging.debug(f'TIDAL: Using ffmpeg from script dir: {res}')
                return res
    except Exception:
        pass
    # 3) config/settings.json ffmpeg_path
    try:
        import json
        path = os.path.join('config', 'settings.json')
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                s = json.load(f)
            p = (s.get('global') or s.get('globals') or {}).get('advanced') or {}
            custom = (p.get('ffmpeg_path') or '').strip()
            if custom and custom.lower() != 'ffmpeg' and os.path.isfile(custom):
                res = os.path.abspath(custom)
                logging.debug(f'TIDAL: Using ffmpeg from config: {res}')
                return res
    except Exception:
        pass
    logging.debug('TIDAL: Using ffmpeg from PATH')
    return 'ffmpeg'

from datetime import datetime
from getpass import getpass
from dataclasses import dataclass
from shutil import copyfileobj
from xml.etree import ElementTree
from tqdm import tqdm

from utils.exceptions import InvalidInput
from utils.models import *
import os
import tempfile
from utils.utils import sanitise_name, silentremove, download_to_temp, create_temp_filename, create_requests_session, get_primary_artist
from .mqa_identifier_python.mqa_identifier_python.mqa_identifier import MqaIdentifier
from .tidal_api import TidalTvSession, TidalApi, TidalMobileSession, TidalGuestSession, SessionType, TidalError, TidalRequestError

def _bool_setting(settings: dict, key: str, default: bool) -> bool:
    """Read a boolean from settings; handle string values from JSON/GUI (e.g. 'False'/'True')."""
    v = settings.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes')
    return bool(v)


module_information = ModuleInformation(
    service_name='TIDAL',
    module_supported_modes=ModuleModes.download | ModuleModes.credits | ModuleModes.covers | ModuleModes.lyrics,
    login_behaviour=ManualEnum.manual,
    global_settings={
        'tv_atmos_token': '4N3n6Q1x95LL5K7p',
        'tv_atmos_secret': 'oKOXfJW371cX6xaZ0PyhgGNBdNLlBZd4AKKYougMjik=',
        'mobile_atmos_hires_token': 'km8T1xS355y7dd3H',
        'mobile_hires_token': '6BDSRdpK9hqEBTgU',
        'guest_token': 'txNoH4kkV41MfH25',
        'guest_secret': 'dQjy0MinCEvxi1O4UmxvxWnDjt4cgHBPw8ll6nYBk98=',
        'enable_mobile': True,
        'prefer_ac4': False,
        'fix_mqa': True
    },
    # currently too broken to keep it, cover needs to be jpg else crash, problems on termux due to pillow
    # flags=ModuleFlags.needs_cover_resize,
    session_storage_variables=['sessions'],
    netlocation_constant='tidal',
    test_url='https://tidal.com/browse/track/92265335',
    url_decoding=ManualEnum.manual,
)


@dataclass
class AudioTrack:
    codec: CodecEnum
    sample_rate: int
    bitrate: int
    urls: list


class ModuleInterface:
    # noinspection PyTypeChecker
    def __init__(self, module_controller: ModuleController):
        self.module_controller = module_controller
        self.cover_size = module_controller.orpheus_options.default_cover_options.resolution
        self.oprinter = module_controller.printer_controller
        self.print = module_controller.printer_controller.oprint
        self.disable_subscription_check = module_controller.orpheus_options.disable_subscription_check
        self.settings = module_controller.module_settings
        self._auth_lock = threading.Lock()

        # LOW = 96kbit/s AAC, HIGH = 320kbit/s AAC, LOSSLESS = 44.1/16 FLAC, HI_RES <= 48/24 FLAC with MQA
        self.quality_parse = {
            QualityEnum.MINIMUM: 'LOW',
            QualityEnum.LOW: 'LOW',
            QualityEnum.MEDIUM: 'HIGH',
            QualityEnum.HIGH: 'HIGH',
            QualityEnum.LOSSLESS: 'LOSSLESS',
            QualityEnum.HIFI: 'HI_RES_LOSSLESS',
            QualityEnum.ATMOS: 'HI_RES_LOSSLESS'
        }

        # save all the TidalSession objects
        self.available_sessions = [SessionType.TV.name, SessionType.MOBILE_DEFAULT.name, SessionType.MOBILE_ATMOS.name]

        # load all saved sessions (TV, Mobile Atmos, Mobile Default)
        saved_sessions = module_controller.temporary_settings_controller.read('sessions')
        if not saved_sessions:
            saved_sessions = {}

        if not _bool_setting(self.settings, 'enable_mobile', True):
            self.available_sessions = [SessionType.TV.name]

        # Check if we should use guest mode (if no sessions exist)
        use_guest = not saved_sessions
        is_gui_mode = os.environ.get('ORPHEUS_GUI') == '1'

        while True:
            sessions = {}
            login_session = None

            def auth_and_save_session(session, session_type):
                session = self.auth_session(session, session_type, login_session)

                # guest sessions are not saved
                if session_type != SessionType.GUEST.name:
                    # get the dict representation from the TidalSession object and save it into saved_session/loginstorage
                    saved_sessions[session_type] = session.get_storage()
                    module_controller.temporary_settings_controller.set('sessions', saved_sessions)
                return session

            if use_guest:
                logging.debug(f'{module_information.service_name}: No saved sessions, initializing guest session')
                guest_session_type = SessionType.GUEST.name
                sessions[guest_session_type] = auth_and_save_session(self.init_session(guest_session_type), guest_session_type)
                # For guest mode, we only need this one session
                break

            # ask for login if there are no saved sessions
            if not saved_sessions:
                if is_gui_mode:
                    logging.debug(f"{module_information.service_name}: No saved sessions in GUI mode, falling back to guest")
                    use_guest = True
                    continue

                login_session_type = None
                if len(self.available_sessions) == 1:
                    login_session_type = self.available_sessions[0]
                else:
                    self.print(f'{module_information.service_name}: Choose a login method:')
                    self.print(f'{module_information.service_name}: 1. TV (browser)')
                    self.print(
                        f"{module_information.service_name}: 2. Mobile (username and password, choose TV if this doesn't work)")

                    while not login_session_type:
                        input_str = input(' Login method: ')
                        try:
                            login_session_type = {
                                '1': SessionType.TV.name,
                                'tv': SessionType.TV.name,
                                '2': SessionType.MOBILE_DEFAULT.name,
                                'mobile': SessionType.MOBILE_DEFAULT.name,
                            }[input_str.lower()]
                        except KeyError:
                            self.print(f'{module_information.service_name}: Invalid choice, try again')

                login_session = auth_and_save_session(self.init_session(login_session_type), login_session_type)

            for session_type in self.available_sessions:
                sessions[session_type] = self.init_session(session_type)

                if session_type in saved_sessions:
                    logging.debug(f'{module_information.service_name}: {session_type} session found, loading')

                    # load the dictionary from the temporary_settings_controller inside the TidalSession class
                    sessions[session_type].set_storage(saved_sessions[session_type])
                else:
                    logging.debug(
                        f'{module_information.service_name}: No {session_type} session found, creating new one')
                    sessions[session_type] = auth_and_save_session(sessions[session_type], session_type)

                # always try to refresh session
                if not sessions[session_type].valid():
                    sessions[session_type].refresh()
                    # Save the refreshed session in the temporary settings
                    saved_sessions[session_type] = sessions[session_type].get_storage()
                    module_controller.temporary_settings_controller.set('sessions', saved_sessions)

                # check for a valid subscription
                try:
                    subscription_info = sessions[session_type].get_subscription()
                    subscription = self.check_subscription(subscription_info)
                except Exception as e:
                    logging.debug(f"{module_information.service_name}: Failed to check subscription: {e}")
                    subscription = False

                if not subscription:
                    if is_gui_mode:
                        logging.debug(f"{module_information.service_name}: Subscription invalid or check failed in GUI mode – falling back to guest")
                        use_guest = True
                        saved_sessions = {} # Clear so we go to guest mode
                        break

                    confirm = input(' Do you want to relogin? [Y/n]: ')

                    if confirm.upper() == 'N':
                        self.print('Exiting...')
                        exit()

                    # reset saved sessions and loop back to login
                    saved_sessions = {}
                    break

                if not login_session:
                    login_session = sessions[session_type]

            if saved_sessions:
                break

        # only needed for region locked albums where the track is available but force_album_format is used
        self.album_cache = {}

        # load the Tidal session with all saved sessions (TV, Mobile Atmos, Mobile Default)
        self.session: TidalApi = TidalApi(sessions)
        if use_guest:
            self.session.default = SessionType.GUEST

    def init_session(self, session_type):
        session = None
        # initialize session with the needed API keys
        if session_type == SessionType.TV.name:
            session = TidalTvSession(
                self.settings.get('tv_atmos_token', '4N3n6Q1x95LL5K7p'),
                self.settings.get('tv_atmos_secret', 'oKOXfJW371cX6xaZ0PyhgGNBdNLlBZd4AKKYougMjik=')
            )
        elif session_type == SessionType.MOBILE_ATMOS.name:
            session = TidalMobileSession(self.settings.get('mobile_atmos_hires_token', 'km8T1xS355y7dd3H'))
        elif session_type == SessionType.GUEST.name:
            session = TidalGuestSession(
                self.settings.get('guest_token', 'txNoH4kkV41MfH25'),
                self.settings.get('guest_secret', 'dQjy0MinCEvxi1O4UmxvxWnDjt4cgHBPw8ll6nYBk98=')
            )
        else:
            session = TidalMobileSession(self.settings.get('mobile_hires_token', '6BDSRdpK9hqEBTgU'))
        return session

    def auth_session(self, session, session_type, login_session):
        if login_session:
            # refresh tokens can be used with any client id
            # this can be used to switch to any client type from an existing session
            session.refresh_token = login_session.refresh_token
            session.user_id = login_session.user_id
            session.country_code = login_session.country_code
            session.refresh()
        elif session_type == SessionType.TV.name:
            self.print(f'{module_information.service_name}: Creating a TV session')
            session.auth()
        elif session_type == SessionType.GUEST.name:
            logging.debug(f'{module_information.service_name}: Creating a Guest session')
            session.auth()
        else:
            self.print(f'{module_information.service_name}: Creating a Mobile session')
            self.print(f'{module_information.service_name}: Enter your TIDAL username and password:')
            self.print(f'{module_information.service_name}: (password will not be echoed)')
            username = input(' Username: ')
            password = getpass(' Password: ')
            session.auth(username, password)
            self.print(f'Successfully logged in, using {session_type} token!')

        return session

    def check_subscription(self, subscription: str) -> bool:
        # returns true if "disable_subscription_checks" is enabled or subscription is HIFI (Plus)
        sub = subscription.upper()
        if not self.disable_subscription_check and not ('HIFI' in sub or 'PREMIUM' in sub or 'TIDAL_PLUS' in sub):
            self.print(f'{module_information.service_name}: Account does not have a HiFi (Plus) subscription, '
                       f'detected subscription: {subscription}')
            return False
        return True

    def is_authenticated(self) -> bool:
        """Return True if we have at least one real (non-guest) session."""
        return any(s != SessionType.GUEST.name for s in self.session.sessions)

    def _is_guest_only(self) -> bool:
        """Return True if only a Guest session exists."""
        return SessionType.GUEST.name in self.session.sessions and len(self.session.sessions) == 1

    def ensure_can_download(self) -> bool:
        """
        Called by music_downloader.py before starting a collection download.
        Ensures we have a real session if required by the quality setting.
        """
        quality_tier = self.module_controller.orpheus_options.quality_tier
        # Force login if quality is better than LOW
        force_login = quality_tier not in {QualityEnum.MINIMUM, QualityEnum.LOW}
        if force_login:
            self._ensure_credentials(force=True)
        return True

    def _ensure_credentials(self, force=False):
        # Use lock to prevent concurrent triggers of the browser/login flow
        with self._auth_lock:
            # Re-check guest status inside the lock
            if not self._is_guest_only():
                return

            # Temporarily reset default to GUEST so API calls still work during login
            # (get_track_info may have set default to TV before calling us)
            previous_default = self.session.default
            self.session.default = SessionType.GUEST

            try:
                # In GUI mode, we usually skip the prompt to allow "quiet" browsing in guest mode.
                # However, if 'force' is True (e.g. during a real download), we proceed to prompt.
                is_gui_mode = os.environ.get('ORPHEUS_GUI') == '1'
                if is_gui_mode and not force:
                    logging.debug(f'{module_information.service_name}: GUI guest mode, skipping auto-login prompt')
                    return

                self.print(f'{module_information.service_name}: Credentials missing. Please login to download tracks.', drop_level=1)
                # Prompt for login
                saved_sessions = {}
                while True:
                    login_session_type = None
                    is_gui_mode = os.environ.get('ORPHEUS_GUI') == '1'
                    if is_gui_mode:
                        # Otherwise, auto-select TV (browser) for first-time login
                        self.print(f'{module_information.service_name}: GUI mode detected, automatically using TV (browser) login.')
                        login_session_type = SessionType.TV.name
                    else:
                        # CLI mode: always show the login method prompt
                        self.print(f'{module_information.service_name}: Choose a login method:')
                        self.print(f'{module_information.service_name}: 1. TV (browser)')
                        self.print(
                            f"{module_information.service_name}: 2. Mobile (username and password, choose TV if this doesn't work)")

                        while not login_session_type:
                            input_str = input(' Login method: ')
                            try:
                                login_session_type = {
                                    '1': SessionType.TV.name,
                                    'tv': SessionType.TV.name,
                                    '2': SessionType.MOBILE_DEFAULT.name,
                                    'mobile': SessionType.MOBILE_DEFAULT.name,
                                }[input_str.lower()]
                            except KeyError:
                                self.print(f'{module_information.service_name}: Invalid choice, try again')

                    # Re-initialize all sessions after login
                    login_session = self.auth_session(self.init_session(login_session_type), login_session_type, None)
                    saved_sessions[login_session_type] = login_session.get_storage()
                    
                    # Also auth other mobile sessions if mobile was chosen
                    for session_type in self.available_sessions:
                        if session_type not in saved_sessions:
                            session = self.init_session(session_type)
                            session.refresh_token = login_session.refresh_token
                            session.user_id = login_session.user_id
                            session.country_code = login_session.country_code
                            session.refresh()
                            saved_sessions[session_type] = session.get_storage()
                    
                    # Save to temporary settings
                    self.module_controller.temporary_settings_controller.set('sessions', saved_sessions)
                    
                    # Re-initialize the API with new sessions
                    new_sessions = {}
                    for st in self.available_sessions:
                        s = self.init_session(st)
                        s.set_storage(saved_sessions[st])
                        new_sessions[st] = s
                    
                    self.session.sessions = new_sessions
                    self.session.default = SessionType.TV # reset default to TV after successful login
                    break
            finally:
                # Restore previous default if we are still in guest mode (e.g. if is_gui_mode and not force)
                if self._is_guest_only():
                    self.session.default = previous_default

    @staticmethod
    def _generate_artwork_url(cover_id: str, size: int, max_size: int = 1280):
        # not the best idea, but it rounds the self.cover_size to the nearest number in supported_sizes, 1281 is needed
        # for the "uncompressed" cover
        supported_sizes = [80, 160, 320, 480, 640, 1080, 1280, 1281]
        best_size = min(supported_sizes, key=lambda x: abs(x - size))
        # only supports 80x80, 160x160, 320x320, 480x480, 640x640, 1080x1080 and 1280x1280 only for non playlists
        # return "uncompressed" cover if self.cover_resolution > max_size
        image_name = '{0}x{0}.jpg'.format(best_size) if best_size <= max_size else 'origin.jpg'
        return f'https://resources.tidal.com/images/{cover_id.replace("-", "/")}/{image_name}'

    @staticmethod
    def _generate_animated_artwork_url(cover_id: str, size=1280):
        return 'https://resources.tidal.com/videos/{0}/{1}x{1}.mp4'.format(cover_id.replace('-', '/'), size)

    def custom_url_parse(self, link: str):
        # Match tidal.com and listen.tidal.com (e.g. https://listen.tidal.com/track/232917499)
        match = re.search(
            r'https?://(?:www\.|listen\.)?tidal\.com/(?:browse/)?'
            r'(?P<media_type>track|album|playlist|artist)/'
            r'(?P<media_id>[A-Za-z0-9-]+)',
            link,
        )
        media_types = {
            'track': DownloadTypeEnum.track,
            'album': DownloadTypeEnum.album,
            'playlist': DownloadTypeEnum.playlist,
            'artist': DownloadTypeEnum.artist,
        }
        if not match:
            raise InvalidInput(f'Unsupported URL: {link}')
        return MediaIdentification(
            media_type=media_types[match.group('media_type')],
            media_id=match.group('media_id'),
        )

    def _merge_user_playlist_search_hits(self, query: str, catalog_items: list, limit: int) -> list:
        """Prepend user-created playlists whose titles match query (catalog search omits these)."""
        if not query or not query.strip() or not self.is_authenticated():
            return catalog_items
        auth = self.session.authenticated_session()
        if not auth or not auth.user_id:
            return catalog_items
        q_lower = query.strip().lower()
        user_hits = []
        try:
            for entry in self.session.iter_user_playlist_entries(auth.user_id):
                if entry.get('type') != 'USER_CREATED':
                    continue
                pl = entry.get('playlist')
                if not isinstance(pl, dict):
                    continue
                title = (pl.get('title') or '').strip()
                if not title or q_lower not in title.lower():
                    continue
                user_hits.append(pl)
        except Exception as e:
            logging.warning('%s: user playlist search failed: %s', module_information.service_name, e)
            return catalog_items
        if not user_hits:
            return catalog_items
        seen = set()
        merged = []
        for i in user_hits + (catalog_items or []):
            uid = i.get('uuid')
            if uid:
                if uid in seen:
                    continue
                seen.add(uid)
            merged.append(i)
            if len(merged) >= limit:
                break
        return merged

    def search(self, query_type: DownloadTypeEnum, query: str, track_info: TrackInfo = None, limit: int = 20):
        if track_info and track_info.tags.isrc:
            results = self.session.get_tracks_by_isrc(track_info.tags.isrc)
            result_items = results.get('items') if isinstance(results, dict) else None
        else:
            results = self.session.get_search_data(query, limit=limit)[query_type.name + 's']
            result_items = results.get('items')
            if query_type is DownloadTypeEnum.playlist:
                result_items = self._merge_user_playlist_search_hits(query, result_items, limit)

        items = []
        for i in result_items or []:
            duration, name = None, None
            image_url = None
            preview_url = None
            playlist_track_count = None

            if query_type is DownloadTypeEnum.artist:
                name = i.get('name')
                artists = None
                year = None
                # Artist image (if available) - check multiple possible field names
                # Tidal API may use 'picture', 'image', 'squareImage', or 'cover' for artist images
                # Also check nested structures that might contain image data
                # Default fallback image UUID (same as python-tidal library uses)
                DEFAULT_ARTIST_IMG = "1e01cdb6-f15d-4d8b-8440-a047976c1cac"
                artist_image_id = (
                    i.get('picture') or 
                    i.get('image') or 
                    i.get('squareImage') or 
                    i.get('cover') or
                    i.get('pictureId') or
                    i.get('imageId') or
                    (i.get('images', {}).get('picture') if isinstance(i.get('images'), dict) else None) or
                    (i.get('images', {}).get('cover') if isinstance(i.get('images'), dict) else None) or
                    (i.get('images', {}).get('squareImage') if isinstance(i.get('images'), dict) else None) or
                    DEFAULT_ARTIST_IMG  # Fallback to default image if none found
                )
                if artist_image_id:
                    # Use 750x750.jpg for artists as it's more reliably available
                    image_url = f'https://resources.tidal.com/images/{artist_image_id.replace("-", "/")}/750x750.jpg'
                # Note: If artist image is not in search results, lazy-loading in GUI will fetch it
                # Note: If artist image is not in search results, it may need to be fetched
                # separately via get_artist() API call. The lazy-loading in GUI will handle this.
            elif query_type is DownloadTypeEnum.playlist:
                creator = i.get('creator') or {}
                if creator.get('name'):
                    artists = [creator.get('name')]
                elif i.get('type') == 'EDITORIAL':
                    artists = [module_information.service_name]
                elif i.get('type') == 'USER':
                    artists = ['You']
                else:
                    artists = ['Unknown']

                duration = i.get('duration')
                # TODO: Use playlist creation date or lastUpdated?
                year = i.get('created')[:4]
                playlist_track_count = i.get('numberOfTracks') or i.get('number_of_tracks')
                # Playlist cover image - check multiple possible field names
                square_image_id = (
                    i.get('squareImage') or
                    i.get('image') or
                    i.get('cover') or
                    i.get('picture')
                )
                if square_image_id:
                    image_url = self._generate_artwork_url(square_image_id, size=56, max_size=1080)
                # Debug: Log available fields if squareImage is missing
                # Uncomment to debug playlist cover extraction:
                # if not square_image_id:
                #     print(f"[Tidal Debug] Playlist '{i.get('title', 'Unknown')}': No squareImage found. Available keys: {list(i.keys())}")
            elif query_type is DownloadTypeEnum.track:
                artists = [j.get('name') for j in i.get('artists')]
                # Getting the year from the album?
                album_data = i.get('album') or {}
                year = album_data.get('releaseDate')[:4] if album_data.get('releaseDate') else None
                duration = i.get('duration')
                # Track cover image (from album) - use 56px for search thumbnails
                if album_data.get('cover'):
                    image_url = self._generate_artwork_url(album_data.get('cover'), size=56)
                # Preview URL - use previewUrl from search results if available
                # The GUI handles lazy-loading via get_preview_stream_url for tracks without it
                preview_url = i.get('previewUrl')
            elif query_type is DownloadTypeEnum.album:
                artists = [j.get('name') for j in i.get('artists')]
                duration = i.get('duration')
                year = i.get('releaseDate')[:4]
                # Album cover image - use 56px for search thumbnails
                if i.get('cover'):
                    image_url = self._generate_artwork_url(i.get('cover'), size=56)
            else:
                raise Exception('Query type is invalid')

            if query_type is not DownloadTypeEnum.artist:
                name = i.get('title')
                name += f' ({i.get("version")})' if i.get("version") else ''

            additional = []
            if query_type not in {DownloadTypeEnum.artist, DownloadTypeEnum.playlist}:
                formatted = self._format_additional_info(i)
                additional = [formatted] if formatted else None
            elif query_type is DownloadTypeEnum.playlist and playlist_track_count is not None:
                additional = [f"1 track" if playlist_track_count == 1 else f"{playlist_track_count} tracks"]

            # Hide playlists with no tracks (e.g. video-only playlists)
            if query_type is DownloadTypeEnum.playlist and (playlist_track_count is None or playlist_track_count == 0):
                continue

            item = SearchResult(
                name=name,
                artists=artists,
                year=year,
                result_id=str(i.get('id')) if query_type is not DownloadTypeEnum.playlist else i.get('uuid'),
                explicit=i.get('explicit'),
                duration=duration,
                additional=(additional if isinstance(additional, list) else [additional]) if additional else None,
                image_url=image_url,
                preview_url=preview_url,
                extra_kwargs={'raw_result': i}  # Store raw API response for full-size cover URL generation
            )

            items.append(item)

        return items

    def explore(self, format_name: str, content_type: str, limit: int = 25):
        """Browse Dolby Atmos releases (tracks, albums, or playlists) via Tidal pages API.
        format_name: 'atmos'. content_type: 'tracks', 'albums', or 'playlists'.
        Returns list of SearchResult with extra_kwargs.media_type set to track, album, or playlist."""
        # Map to page and possible module titles (API may use different wording)
        page_titles = {
            ('atmos', 'tracks'): (('dolby_atmos', 'atmos'), ('Tracks', 'New Tracks', 'Dolby Atmos Tracks')),
            ('atmos', 'albums'): (('dolby_atmos', 'atmos'), ('New Albums', 'Albums', 'Dolby Atmos Albums')),
            ('atmos', 'playlists'): (('dolby_atmos', 'atmos'), ('Playlists', 'Dolby Atmos Playlists', 'Curated Playlists')),
        }
        key = (format_name.lower(), content_type.lower())
        if key not in page_titles:
            return []
        page_options, title_options = page_titles[key]
        page_options = (page_options,) if isinstance(page_options, str) else page_options
        title_options = (title_options,) if isinstance(title_options, str) else title_options

        page_content = None
        page_used = None
        for page in page_options:
            try:
                page_content = self.session.get_page(page)
                page_used = page
                break
            except Exception as e:
                continue
        
        if not page_content:
            error_msg = f"Tidal explore: Failed to fetch any page from {page_options}"
            logging.getLogger(__name__).warning(error_msg)
            print(f"[Tidal Explore] {error_msg}")
            return []

        rows = page_content.get('rows') or []
        show_more_path = None
        for row in rows:
            for mod in row.get('modules') or []:
                mod_title = (mod.get('title') or '').strip()
                if mod_title in title_options:
                    show_more = mod.get('showMore') or {}
                    api_path = (show_more.get('apiPath') or '').strip()
                    if api_path:
                        path = api_path.lstrip('/')
                        if path.startswith('v1/'):
                            path = path[3:]
                        show_more_path = path
                        break
            if show_more_path:
                break

        if not show_more_path:
            # Collect available module titles for debugging
            available_titles = []
            for row in rows:
                for mod in row.get('modules') or []:
                    title = mod.get('title')
                    if title:
                        available_titles.append(title)
            error_msg = (
                f"Tidal explore: Could not find module with title matching {title_options} "
                f"on page '{page_used}'. Available titles: {available_titles[:10]}"
            )
            logging.getLogger(__name__).warning(error_msg)
            print(f"[Tidal Explore] {error_msg}")
            return []

        try:
            single_page = self.session.get_path(show_more_path)
        except Exception as e1:
            if not show_more_path.startswith('pages/'):
                try:
                    single_page = self.session.get_path('pages/' + show_more_path)
                except Exception as e2:
                    error_msg = f"Tidal explore: Failed to fetch path '{show_more_path}' and 'pages/{show_more_path}': {e1}, {e2}"
                    logging.getLogger(__name__).warning(error_msg)
                    print(f"[Tidal Explore] {error_msg}")
                    return []
            else:
                error_msg = f"Tidal explore: Failed to fetch path '{show_more_path}': {e1}"
                logging.getLogger(__name__).warning(error_msg)
                print(f"[Tidal Explore] {error_msg}")
                return []

        rows2 = single_page.get('rows') or []
        if not rows2:
            error_msg = f"Tidal explore: No rows in response from path '{show_more_path}'"
            logging.getLogger(__name__).warning(error_msg)
            print(f"[Tidal Explore] {error_msg}")
            return []
        modules2 = (rows2[0].get('modules') or [{}])
        paged_list = None
        for m in modules2:
            paged_list = m.get('pagedList')
            if paged_list:
                break
        if not paged_list:
            error_msg = f"Tidal explore: No pagedList found in response from '{show_more_path}'"
            logging.getLogger(__name__).warning(error_msg)
            print(f"[Tidal Explore] {error_msg}")
            return []

        total = paged_list.get('totalNumberOfItems', 0)
        # Collect any items already in the first response (some modules put the first page here)
        initial_items = []
        for m in modules2:
            initial_items.extend(m.get('items') or [])
        items = list(initial_items)

        data_api_path = (paged_list.get('dataApiPath') or '').strip()
        if not data_api_path:
            error_msg = f"Tidal explore: No dataApiPath in pagedList (total={total})"
            logging.getLogger(__name__).warning(error_msg)
            print(f"[Tidal Explore] {error_msg}")
            return []
        path = data_api_path.lstrip('/')
        if path.startswith('v1/'):
            path = path[3:]

        page_size = 50
        pagination_path = path if path.startswith('pages/') else 'pages/' + path
        # Paginate from after initial items so we don't duplicate; request up to limit
        start_offset = len(initial_items)
        effective_total = max(total, start_offset)  # in case total was underreported
        for offset in range(start_offset, min(effective_total, limit), page_size):
            if len(items) >= limit:
                break
            try:
                resp = self.session.get_path(pagination_path, params={'offset': offset, 'limit': page_size})
            except Exception:
                try:
                    resp = self.session.get_path(path, params={'offset': offset, 'limit': page_size})
                except Exception:
                    break
            batch = resp.get('items') or []
            for it in batch:
                if len(items) >= limit:
                    break
                items.append(it)

        results = []
        is_playlist_content = content_type.lower() == 'playlists'
        for i in items:
            if is_playlist_content:
                # Playlist item: uuid, title, creator, numberOfTracks, squareImage, duration, created
                media_type = DownloadTypeEnum.playlist
                name = (i.get('title') or '')
                if i.get('version'):
                    name += f' ({i["version"]})'
                creator = i.get('creator') or {}
                artists = [creator.get('name')] if creator.get('name') else (['Tidal'] if i.get('type') == 'EDITORIAL' else ['Unknown'])
                year = None
                created = i.get('created')
                if created:
                    year = str(created)[:4]
                duration = i.get('duration')
                image_url = None
                cover_id = i.get('squareImage') or i.get('image') or i.get('cover') or i.get('picture')
                if cover_id:
                    image_url = self._generate_artwork_url(cover_id, size=56, max_size=1080)
                track_count = i.get('numberOfTracks') or i.get('number_of_tracks')
                additional = f"{track_count} tracks" if track_count is not None else None
                result_id = i.get('uuid') or str(i.get('id', ''))
            else:
                is_album = 'album' in (i.get('url') or '')
                media_type = DownloadTypeEnum.album if is_album else DownloadTypeEnum.track

                name = (i.get('title') or '')
                if i.get('version'):
                    name += f' ({i["version"]})'
                artists = [j.get('name') for j in (i.get('artists') or []) if j.get('name')]
                if not artists:
                    artists = ['Unknown']

                year = None
                release_date = i.get('releaseDate') or i.get('streamStartDate')
                if release_date:
                    year = str(release_date)[:4]
                duration = i.get('duration')

                image_url = None
                cover_id = i.get('cover') or (i.get('album') or {}).get('cover')
                if cover_id:
                    image_url = self._generate_artwork_url(cover_id, size=56)

                additional_list = []
                audio_modes = i.get('audioModes') or []
                tags = i.get('mediaMetadata', {}).get('tags', [])
                
                if 'DOLBY_ATMOS' in tags or 'DOLBY_ATMOS' in audio_modes:
                    additional_list.append('◗◖ ATMOS')
                if 'SONY_360RA' in tags or 'SONY_360RA' in audio_modes:
                    additional_list.append('360 Reality Audio')
                if 'HIRES_LOSSLESS' in tags or i.get('audioQuality') == 'HI_RES':
                    additional_list.append('🅷 HI-RES')
                elif (
                    i.get('audioQuality') in ('LOSSLESS', 'LOSSLESS_STEREO')
                    or 'LOSSLESS' in tags
                ) and not additional_list:
                    additional_list.append('FLAC')

                additional = " / ".join(additional_list) if additional_list else None

                result_id = str(i.get('id', ''))

            results.append(SearchResult(
                name=name,
                artists=artists,
                year=year,
                result_id=result_id,
                explicit=i.get('explicit', False),
                duration=duration,
                additional=[additional] if additional else None,
                image_url=image_url,
                preview_url=None,
                extra_kwargs={'raw_result': i, 'media_type': media_type},
            ))

        return results

    def get_playlist_info(self, playlist_id: str) -> PlaylistInfo:
        playlist_data = self.session.get_playlist(playlist_id)
        playlist_tracks = self.session.get_playlist_items(playlist_id)

        tracks = [track.get('item').get('id') for track in playlist_tracks.get('items') if track.get('type') == 'track']
        track_data_dict = {}
        for track in playlist_tracks.get('items'):
            item = track.get('item')
            if item:
                item['additional'] = self._format_additional_info(item)
                track_data_dict[item.get('id')] = item

        if 'name' in playlist_data.get('creator'):
            creator_name = playlist_data.get('creator').get('name')
        elif playlist_data.get('type') == 'EDITORIAL':
            creator_name = module_information.service_name
        else:
            creator_name = 'Unknown'

        if playlist_data.get('squareImage'):
            cover_url = self._generate_artwork_url(playlist_data['squareImage'], size=self.cover_size, max_size=1080)
            cover_type = ImageFileTypeEnum.jpg
        else:
            # fallback to defaultPlaylistImage
            cover_url = 'https://tidal.com/browse/assets/images/defaultImages/defaultPlaylistImage.png'
            cover_type = ImageFileTypeEnum.png

        # Playlist items already include title/duration; build TrackInfo so expand skips per-track API calls.
        if track_data_dict:
            track_objects = []
            for tid in tracks:
                td = track_data_dict.get(tid, {})
                track_name = td.get('title', 'Unknown')
                if td.get('version'):
                    track_name += f' ({td["version"]})'
                track_cover = cover_url
                if td.get('album', {}).get('cover'):
                    track_cover = self._generate_artwork_url(td['album']['cover'], size=self.cover_size)
                track_objects.append(TrackInfo(
                    name=track_name,
                    album=td.get('album', {}).get('title', ''),
                    album_id=str(td.get('album', {}).get('id', '')),
                    artists=[a.get('name') for a in td.get('artists', []) if a.get('name')],
                    tags=Tags(),
                    codec=CodecEnum.FLAC,
                    cover_url=track_cover,
                    release_year=str(td.get('streamStartDate', ''))[:4] if td.get('streamStartDate') else None,
                    duration=td.get('duration'),
                    explicit=td.get('explicit'),
                    id=str(tid),
                    additional=td.get('additional')
                ))
            tracks = track_objects

        return PlaylistInfo(
            name=playlist_data.get('title'),
            creator=creator_name,
            tracks=tracks,
            release_year=playlist_data.get('created')[:4],
            duration=playlist_data.get('duration'),
            creator_id=playlist_data['creator'].get('id'),
            cover_url=cover_url,
            cover_type=cover_type,
            track_extra_kwargs={
                'data': track_data_dict
            }
        )

    def get_artist_info(self, artist_id: str, get_credited_albums: bool) -> ArtistInfo:
        artist_data = self.session.get_artist(artist_id)

        artist_albums = self.session.get_artist_albums(artist_id).get('items')
        artist_singles = self.session.get_artist_albums_ep_singles(artist_id).get('items')

        # Only works with a mobile session, annoying, never do this again
        credit_albums = []
        if get_credited_albums:
            previous_default = self.session.default
            try:
                # Only attempt if MOBILE_DEFAULT session exists (requires login)
                if SessionType.MOBILE_DEFAULT.name in self.session.sessions:
                    self.session.default = SessionType.MOBILE_DEFAULT
                    credited_albums_page = self.session.get_page('contributor', params={'artistId': artist_id})

                    # This is so retarded
                    page_list = credited_albums_page['rows'][-1]['modules'][0].get('pagedList')
                    if page_list:
                        total_items = page_list['totalNumberOfItems']
                        more_items_link = page_list['dataApiPath'][6:]

                        # Now fetch all the found total_items
                        items = []
                        for offset in range(0, total_items // 50 + 1):
                            self.print(f'Fetching artist releases (page {offset + 1}/{total_items // 50 + 1})', end='\r')
                            items += self.session.get_page(more_items_link, params={'limit': 50, 'offset': offset * 50})[
                                'items']

                        credit_albums = [item.get('item').get('album') for item in items]
            except Exception as e:
                logging.debug(f'{module_information.service_name}: Could not fetch credited albums: {e}')
            finally:
                self.session.default = previous_default

        # use set to filter out duplicate album ids
        albums = {str(album.get('id')) for album in artist_albums + artist_singles + credit_albums if album}
        
        # Build detailed album list for GUI
        albums_out = []
        all_album_dicts_cache = {}
        for a in artist_albums + artist_singles + credit_albums:
            if a and a.get('id') is not None:
                aid = str(a.get('id'))
                if aid not in all_album_dicts_cache:
                    # Add formatted traits to the album dict so it shows in GUI expanded list
                    a['additional'] = self._format_additional_info(a)
                    all_album_dicts_cache[aid] = a
                    
                    release_year = None
                    if a.get('releaseDate'):
                        release_year = a.get('releaseDate')[:4]
                    elif a.get('streamStartDate'):
                        release_year = a.get('streamStartDate')[:4]
                        
                    albums_out.append({
                        'id': aid,
                        'name': a.get('title'),
                        'artist': a.get('artist', {}).get('name') if a.get('artist') else (a.get('artists', [{}])[0].get('name') if a.get('artists') else None),
                        'release_year': release_year,
                        'cover_url': self._generate_artwork_url(a.get('cover'), size=56) if a.get('cover') else None,
                        'additional': a.get('additional'),
                        'explicit': a.get('explicit'),
                        'duration': a.get('duration')
                    })

        # Batch fetch missing metadata (track counts/quality) for albums using ThreadPoolExecutor
        # Especially needed for credited albums which might be barebones
        missing_metadata = [idx for idx, t in enumerate(albums_out) if isinstance(t, dict) and (not t.get('duration') or not t.get('release_year') or not t.get('additional') or t.get('explicit') is not True)]
        if missing_metadata:
            a_meta = {}
            def _fetch_tidal_album_meta(aid):
                try:
                    a_data = self.session.get_album(aid)
                    if a_data:
                        formatted = self._format_additional_info(a_data)
                        return aid, {
                            'additional': formatted,
                            'year': (a_data.get('releaseDate') or a_data.get('streamStartDate') or '')[:4] or None,
                            'explicit': a_data.get('explicit'),
                            'duration': a_data.get('duration'),
                            'raw': a_data
                        }
                except: pass
                return aid, None

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                fetch_ids = [albums_out[idx]['id'] for idx in missing_metadata]
                for aid, meta in executor.map(_fetch_tidal_album_meta, fetch_ids):
                    if meta:
                        a_meta[str(aid)] = meta
            
            for idx in missing_metadata:
                alb = albums_out[idx]
                aid = str(alb['id'])
                if aid in a_meta:
                    alb['additional'] = a_meta[aid]['additional']
                    if not alb.get('release_year'):
                        alb['release_year'] = a_meta[aid]['year']
                    if a_meta[aid].get('explicit'):
                        alb['explicit'] = True
                    elif alb.get('explicit') is None:
                        alb['explicit'] = a_meta[aid]['explicit']
                    
                    if a_meta[aid].get('duration'):
                        alb['duration'] = a_meta[aid]['duration']

                    # Update cache with full data for later use (e.g. get_album_info)
                    all_album_dicts_cache[aid] = a_meta[aid]['raw']
                    all_album_dicts_cache[aid]['additional'] = a_meta[aid]['additional']

        return ArtistInfo(
            name=artist_data.get('name'),
            albums=albums_out if albums_out else list(albums),
            album_extra_kwargs={'data': all_album_dicts_cache}
        )

    def _is_guest_only(self):
        """Check if we only have a guest session (no authenticated user sessions)."""
        return (SessionType.GUEST.name in self.session.sessions and
                len(self.session.sessions) == 1)

    @staticmethod
    def _track_display_title(item: dict) -> str:
        title = (item or {}).get('title') or 'Unknown'
        version = (item or {}).get('version')
        if version:
            title = f'{title} ({version})'
        return title

    @staticmethod
    def _track_artists_from_item(item: dict) -> list:
        return [a.get('name') for a in (item or {}).get('artists', []) if a.get('name')]

    def _merge_tidal_album_exclusions(
        self,
        album_id: str,
        included_track_ids: list,
        cache: dict,
        excluded_tracks: list,
        expected_track_count: int = None,
    ) -> list:
        """Find album tracks/items missing from the download list and return enriched exclusions."""
        included_ids = set()
        for tid in included_track_ids:
            if hasattr(tid, 'id'):
                included_ids.add(str(tid.id))
            else:
                included_ids.add(str(tid))
        seen_ids = {str(e.get('id')) for e in excluded_tracks if e.get('id')}

        def append_exclusion(item: dict, entry_type: str, reason: str):
            item_id = str(item.get('id')) if item.get('id') is not None else None
            if item_id and item_id in seen_ids:
                return
            if item_id:
                seen_ids.add(item_id)
            excluded_tracks.append({
                'id': item_id,
                'name': self._track_display_title(item),
                'artists': self._track_artists_from_item(item),
                'track_number': item.get('trackNumber'),
                'disc_number': item.get('volumeNumber'),
                'reason': reason,
            })

        for source_name, fetch in (
            ('album items', lambda: self.session.get_album_items_all(album_id)),
            ('album tracks', lambda: self.session.get_album_tracks_all(album_id)),
        ):
            try:
                payload = fetch()
            except TidalError:
                continue
            for entry in payload.get('items') or []:
                if isinstance(entry, dict) and entry.get('item') is not None:
                    entry_type = entry.get('type') or 'track'
                    item = entry.get('item') or {}
                else:
                    entry_type = 'track'
                    item = entry if isinstance(entry, dict) else {}
                item_id = str(item.get('id')) if item.get('id') is not None else None
                if not item_id or item_id in included_ids:
                    continue
                if entry_type != 'track':
                    append_exclusion(
                        item,
                        entry_type,
                        f'Not downloaded: {entry_type} item on album ({source_name})',
                    )
                else:
                    append_exclusion(
                        item,
                        entry_type,
                        f'Not downloaded: listed on album via {source_name} but omitted from credits API',
                    )

        excluded_tracks.extend(
            self._detect_tidal_per_disc_track_gaps(cache, seen_ids)
        )

        return excluded_tracks

    def _detect_tidal_per_disc_track_gaps(self, cache: dict, seen_ids: set) -> list:
        """
        Detect missing tracks from gaps in per-disc trackNumber sequences (multi-disc albums
        repeat track 1..N on each disc). Optionally probe sequential track IDs between neighbors.
        """
        from collections import defaultdict

        by_disc = defaultdict(list)
        for tid, td in (cache.get('data') or {}).items():
            track_no = td.get('trackNumber')
            if track_no is None:
                continue
            disc = int(td.get('volumeNumber') or 1)
            by_disc[disc].append((int(track_no), str(tid), td))

        exclusions = []
        logged_positions = set()

        for disc in sorted(by_disc):
            entries = sorted(by_disc[disc], key=lambda row: row[0])
            if len(entries) < 2:
                continue
            nums = [row[0] for row in entries]
            by_num = {num: (tid, td) for num, tid, td in entries}
            for gap_num in range(min(nums), max(nums) + 1):
                if gap_num in by_num:
                    continue
                pos_key = (disc, gap_num)
                if pos_key in logged_positions:
                    continue
                logged_positions.add(pos_key)

                prev_entry = by_num.get(gap_num - 1)
                next_entry = by_num.get(gap_num + 1)
                guessed_id = None
                title = f'(disc {disc} track {gap_num})'
                artists = []
                reason = (
                    f'Missing from album track list (disc {disc} track {gap_num} — '
                    f'not returned by TIDAL items/credits API)'
                )

                if prev_entry and next_entry:
                    try:
                        prev_id = int(prev_entry[0])
                        next_id = int(next_entry[0])
                        if next_id - prev_id == 2:
                            guessed_id = str(prev_id + 1)
                            try:
                                track_payload = self.session.get_track(guessed_id)
                                title = self._track_display_title(track_payload)
                                artists = self._track_artists_from_item(track_payload)
                                reason = (
                                    'Region-locked or unavailable for your account '
                                    f'(catalog id {guessed_id} — present on TIDAL but not streamable here)'
                                )
                            except TidalError as exc:
                                msg = str(exc)
                                if 'region' in msg.lower() or 'not found' in msg.lower():
                                    reason = (
                                        f'Region-locked or unavailable for your account '
                                        f'(catalog id {guessed_id})'
                                    )
                                else:
                                    reason = f'Unavailable (catalog id {guessed_id}: {msg})'
                    except (TypeError, ValueError):
                        pass

                if guessed_id and guessed_id in seen_ids:
                    continue
                if guessed_id:
                    seen_ids.add(guessed_id)

                exclusions.append({
                    'id': guessed_id,
                    'name': title,
                    'artists': artists,
                    'track_number': gap_num,
                    'disc_number': disc,
                    'reason': reason,
                })

        return exclusions

    def get_album_info(self, album_id: str, data=None) -> AlbumInfo:
        # check if album is already in album cache, add it
        if data is None:
            data = {}

        if data.get(album_id):
            album_data = data[album_id]
        elif self.album_cache.get(album_id):
            album_data = self.album_cache[album_id]
        else:
            album_data = self.session.get_album(album_id)

        # get all album tracks with corresponding credits with a limit of 100
        limit = 100
        cache = {'data': {}}
        excluded_tracks = []
        try:
            tracks_data = self.session.get_album_contributors(album_id, limit=limit)
            total_tracks = tracks_data.get('totalNumberOfItems')

            # round total_tracks to the next 100 and loop over the offset, that's hideous
            for offset in range(limit, ((total_tracks // limit) + 1) * limit, limit):
                # fetch the new album tracks with the given offset
                track_items = self.session.get_album_contributors(album_id, offset=offset, limit=limit)
                # append those tracks to the album_data
                tracks_data['items'] += track_items.get('items')

            # add the track contributors to a new list called 'credits'
            cache = {'data': {}}
            for track in tracks_data.get('items'):
                item = track.get('item')
                item.update({'credits': track.get('credits')})
                # Add formatted traits to the track dict
                item['additional'] = self._format_additional_info(item)
                cache.get('data')[str(item.get('id'))] = item

            # filter out video clips and other non-audio items; record exclusions for error.txt
            excluded_tracks = []
            tracks = []
            for entry in tracks_data.get('items') or []:
                entry_type = entry.get('type')
                item = entry.get('item') or {}
                item_id = str(item.get('id')) if item.get('id') is not None else None
                title = item.get('title') or 'Unknown'
                if entry_type != 'track':
                    excluded_tracks.append({
                        'id': item_id,
                        'name': title,
                        'artists': [a.get('name') for a in item.get('artists', []) if a.get('name')],
                        'reason': f'Excluded: not an audio track (type={entry_type or "unknown"})',
                    })
                    continue
                if not item_id or item_id not in cache.get('data', {}):
                    excluded_tracks.append({
                        'id': item_id,
                        'name': title,
                        'artists': [a.get('name') for a in item.get('artists', []) if a.get('name')],
                        'reason': 'Excluded: track metadata unavailable from TIDAL API',
                    })
                    continue
                tracks.append(item_id)
        except TidalError:
            tracks = []
            excluded_tracks = []

        quality_list = []
        album_tags = album_data.get('mediaMetadata', {}).get('tags', [])
        if 'audioModes' in album_data:
            if 'DOLBY_ATMOS' in album_data['audioModes']:
                quality_list.append('◗◖ ATMOS')
            if 'SONY_360RA' in album_data['audioModes']:
                quality_list.append('360 Reality Audio')

        if 'HIRES_LOSSLESS' in album_tags or album_data.get('audioQuality') == 'HI_RES':
            quality_list.append('🅷 HI-RES')
        elif (
            album_data.get('audioQuality') in ('LOSSLESS', 'LOSSLESS_STEREO')
            or 'LOSSLESS' in album_tags
        ) and not quality_list:
            quality_list.append('FLAC')

        quality = " / ".join(quality_list) if quality_list else None

        release_year = None
        if album_data.get('releaseDate'):
            release_year = album_data.get('releaseDate')[:4]
        elif album_data.get('streamStartDate'):
            release_year = album_data.get('streamStartDate')[:4]
        elif album_data.get('copyright'):
            # assume that every copyright includes the year
            release_year = [int(s) for s in album_data.get('copyright').split() if s.isdigit()]
            if len(release_year) > 0:
                release_year = release_year[0]

        if album_data.get('cover'):
            cover_url = self._generate_artwork_url(album_data.get('cover'), size=self.cover_size)
            cover_type = ImageFileTypeEnum.jpg
        else:
            # fallback to defaultAlbumImage
            cover_url = 'https://tidal.com/browse/assets/images/defaultImages/defaultAlbumImage.png'
            cover_type = ImageFileTypeEnum.png

        # In guest-only mode, return TrackInfo objects so the GUI can display the tracklist
        # without calling get_track_info() (which requires login for stream URLs).
        if self._is_guest_only() and cache.get('data'):
            track_objects = []
            for tid in tracks:
                td = cache['data'].get(tid, {})
                track_name = td.get('title', 'Unknown')
                if td.get('version'):
                    track_name += f' ({td["version"]})'
                track_cover = cover_url
                if td.get('album', {}).get('cover'):
                    track_cover = self._generate_artwork_url(td['album']['cover'], size=self.cover_size)
                track_objects.append(TrackInfo(
                    name=track_name,
                    album=album_data.get('title', ''),
                    album_id=album_id,
                    artists=[a.get('name') for a in td.get('artists', []) if a.get('name')],
                    tags=Tags(),
                    codec=CodecEnum.FLAC,
                    cover_url=track_cover,
                    release_year=release_year,
                    duration=td.get('duration'),
                    explicit=td.get('explicit'),
                    id=str(tid),
                    additional=td.get('additional')
                ))
            tracks = track_objects

        # Extract label from credits (using first track) or fallback to copyright
        album_label = None
        if tracks_data and tracks_data.get('items'):
            album_label = self._extract_label_from_credits(tracks_data['items'][0].get('credits'))
        if not album_label:
            album_label = self._extract_label_from_copyright(album_data.get('copyright'))

        expected_track_count = album_data.get('numberOfTracks')
        if expected_track_count is not None:
            try:
                expected_track_count = int(expected_track_count)
            except (TypeError, ValueError):
                expected_track_count = None

        excluded_tracks = self._merge_tidal_album_exclusions(
            album_id,
            tracks,
            cache,
            excluded_tracks,
            expected_track_count=expected_track_count,
        )

        return AlbumInfo(
            name=album_data.get('title'),
            release_year=release_year,
            explicit=album_data.get('explicit'),
            quality=quality,
            upc=album_data.get('upc'),
            label=album_label,
            duration=album_data.get('duration'),
            cover_url=cover_url,
            cover_type=cover_type,
            animated_cover_url=self._generate_animated_artwork_url(album_data.get('videoCover')) if album_data.get(
                'videoCover') else None,
            artist=album_data.get('artist', {}).get('name') if album_data.get('artist') else (album_data.get('artists', [{}])[0].get('name') if album_data.get('artists') else 'Unknown'),
            artist_id=album_data.get('artist', {}).get('id') if album_data.get('artist') else (album_data.get('artists', [{}])[0].get('id') if album_data.get('artists') else None),
            tracks=tracks,
            expected_track_count=expected_track_count,
            excluded_tracks=excluded_tracks or None,
            track_extra_kwargs=cache
        )

    def get_track_info(self, track_id: str, quality_tier: QualityEnum, codec_options: CodecOptions,
                       data=None, **kwargs) -> TrackInfo:
        if data is None:
            data = {}

        track_data = data[track_id] if track_id in data else self.session.get_track(track_id)

        album_id = str(track_data.get('album').get('id'))
        # check if album is already in album cache, get it
        try:
            album_data = data[album_id] if album_id in data else self.session.get_album(album_id)
        except TidalError as e:
            # if an error occurs, catch it and set the album_data to an empty dict to catch it
            self.print(f'{module_information.service_name}: {e} Trying workaround ...', drop_level=1)
            album_data = track_data.get('album')
            album_data.update({
                'artist': track_data.get('artist'),
                'numberOfVolumes': 1,
                'audioQuality': 'LOSSLESS',
                'audioModes': ['STEREO']
            })

            # add the region locked album to the cache in order to properly use it later (force_album_format)
            self.album_cache = {album_id: album_data}

        # Fetch credits if missing (needed for label extract)
        if 'credits' not in track_data:
            try:
                # get_track_contributors returns {items: [...]}
                contributors_resp = self.session.get_track_contributors(track_id)
                track_data['credits'] = (contributors_resp or {}).get('items') or []
            except:
                track_data['credits'] = []

        media_tags = track_data['mediaMetadata']['tags']
        format = None
        if codec_options.spatial_codecs and quality_tier is QualityEnum.ATMOS:
            if 'SONY_360RA' in media_tags:
                format = '360ra'
            elif 'DOLBY_ATMOS' in media_tags:
                # prefer_ac4 False → TV session → E-AC-3 JOC (plays in VLC/Audacity). True → MOBILE_ATMOS → AC-4.
                if _bool_setting(self.settings, 'prefer_ac4', False):
                    format = 'ac4'
                else:
                    format = 'ac3'
        # Both HIFI and ATMOS tiers want true Hi-Res FLAC for tracks that have no spatial mix.
        # Without ATMOS here, a non-Atmos track under the Atmos tier falls through to the TV
        # session and silently downgrades a Hi-Res album to CD-quality LOSSLESS.
        if 'HIRES_LOSSLESS' in media_tags and not format and quality_tier in (QualityEnum.HIFI, QualityEnum.ATMOS):
            format = 'flac_hires'

        session = {
            'flac_hires': SessionType.MOBILE_DEFAULT,
            '360ra': SessionType.MOBILE_DEFAULT,
            'ac4': SessionType.MOBILE_ATMOS,
            'ac3': SessionType.TV,
            # TV is used whenever possible to avoid MPEG-DASH, which slows downloading
            None: SessionType.TV,
        }[format]

        if not format and 'DOLBY_ATMOS' in media_tags:
            # if atmos is available, we don't use the TV session here because that will get atmos everytime
            # there are no tracks with both 360RA and atmos afaik,
            # so this shouldn't be an issue for now
            session = SessionType.MOBILE_DEFAULT

        if session.name in self.available_sessions:
            self.session.default = session
        else:
            format = None

        # define all default values in case the stream_data is None (region locked)
        audio_track, mqa_file, track_codec, bitrate, download_args, error = None, None, CodecEnum.FLAC, None, None, None

        # Ensure we have credentials before fetching stream URL.
        # Force login if quality is better than LOW (which guest mode usually doesn't support well for full tracks)
        # or if we are already in HIFI/ATMOS mode.
        force_login = quality_tier not in {QualityEnum.MINIMUM, QualityEnum.LOW}
        self._ensure_credentials(force=force_login)

        try:
            stream_data = self.session.get_stream_url(track_id, self.quality_parse[
                quality_tier] if format != 'flac_hires' else 'HI_RES_LOSSLESS')
        except (TidalRequestError, TidalError) as e:
            # If guest session failed with 401/403, it's likely that even LOW quality requires a real session now,
            # or the guest session is blocked/expired. Force a login prompt.
            is_auth_error = False
            if isinstance(e, TidalRequestError):
                is_auth_error = e.payload.get('status') in (401, 403)
            elif isinstance(e, TidalError):
                is_auth_error = '401' in str(e) or '403' in str(e)

            if is_auth_error and self._is_guest_only():
                self.print(f'{module_information.service_name}: Guest session insufficient. Triggering login...', drop_level=1)
                self._ensure_credentials(force=True)
                # Retry once after login
                stream_data = self.session.get_stream_url(track_id, self.quality_parse[
                    quality_tier] if format != 'flac_hires' else 'HI_RES_LOSSLESS')
            else:
                error = e
                # definitely region locked
                if 'Asset is not ready for playback' in str(e):
                    error = f'Track [{track_id}] is not available in your region'
                stream_data = None

        if stream_data is not None:
            if stream_data['manifestMimeType'] == 'application/dash+xml':
                manifest = base64.b64decode(stream_data['manifest'])
                audio_track = self.parse_mpd(manifest)[0]  # Only one AudioTrack?
                track_codec = audio_track.codec
            else:
                manifest = json.loads(base64.b64decode(stream_data['manifest']))
                track_codec = CodecEnum['AAC' if 'mp4a' in manifest['codecs'] else manifest['codecs'].upper()]

            if not codec_data[track_codec].spatial:
                if not codec_options.proprietary_codecs and codec_data[track_codec].proprietary:
                    self.print(f'Proprietary codecs are disabled, if you want to download {track_codec.name}, '
                               f'set "proprietary_codecs": true', drop_level=1)
                    stream_data = self.session.get_stream_url(track_id, 'LOSSLESS')

                    if stream_data['manifestMimeType'] == 'application/dash+xml':
                        manifest = base64.b64decode(stream_data['manifest'])
                        audio_track = self.parse_mpd(manifest)[0]  # Only one AudioTrack?
                        track_codec = audio_track.codec
                    else:
                        manifest = json.loads(base64.b64decode(stream_data['manifest']))
                        track_codec = CodecEnum['AAC' if 'mp4a' in manifest['codecs'] else manifest['codecs'].upper()]

            if audio_track:
                download_args = {'audio_track': audio_track}
            else:
                # check if MQA
                if track_codec is CodecEnum.MQA and _bool_setting(self.settings, 'fix_mqa', True):
                    # download the first chunk of the flac file to analyze it
                    temp_file_path = self.download_temp_header(manifest['urls'][0])

                    # detect MQA file
                    mqa_file = MqaIdentifier(temp_file_path)

                # add the file to download_args (include codec so single-URL Atmos can be transcoded to AAC)
                download_args = {'file_url': manifest['urls'][0], 'codec': track_codec}

        # https://en.wikipedia.org/wiki/Audio_bit_depth#cite_ref-1
        bit_depth = (24 if stream_data and stream_data['audioQuality'] == 'HI_RES_LOSSLESS' else 16) \
            if track_codec in {CodecEnum.FLAC, CodecEnum.ALAC} else None
        sample_rate = 48 if track_codec in {CodecEnum.EAC3, CodecEnum.MHA1, CodecEnum.AC4} else 44.1

        if stream_data:
            # fallback bitrate
            bitrate = {
                'LOW': 96,
                'HIGH': 320,
                'LOSSLESS': 1411,
                'HI_RES': None,
                'HI_RES_LOSSLESS': None
            }[stream_data['audioQuality']]

            # manually set bitrate for immersive formats
            if stream_data['audioMode'] == 'DOLBY_ATMOS':
                # check if the Dolby Atmos format is E-AC-3 JOC or AC-4
                if track_codec == CodecEnum.EAC3:
                    bitrate = 768
                elif track_codec == CodecEnum.AC4:
                    bitrate = 256
            elif stream_data['audioMode'] == 'SONY_360RA':
                bitrate = 667

        # more precise bitrate tidal uses MPEG-DASH
        if audio_track:
            bitrate = audio_track.bitrate // 1000
            # Snap Tidal lossy bitrates to standard values for cleaner display
            if 310 <= bitrate <= 330: bitrate = 320
            elif 90 <= bitrate <= 110: bitrate = 96

            if stream_data['audioQuality'] == 'HI_RES_LOSSLESS':
                sample_rate = audio_track.sample_rate / 1000

        # now set everything for MQA
        if mqa_file is not None and mqa_file.is_mqa:
            bit_depth = mqa_file.bit_depth
            sample_rate = mqa_file.get_original_sample_rate()

        track_name = track_data.get('title')
        track_name += f' ({track_data.get("version")})' if track_data.get("version") else ''

        if track_data['album'].get('cover'):
            cover_url = self._generate_artwork_url(track_data['album'].get('cover'), size=self.cover_size)
        else:
            # fallback to defaultTrackImage, no cover_type flag? Might crash in the future
            cover_url = 'https://tidal.com/browse/assets/images/defaultImages/defaultTrackImage.png'

        track_info = TrackInfo(
            name=track_name,
            album=album_data.get('title'),
            album_id=album_id,
            id=str(track_id),
            artists=[a.get('name') for a in track_data.get('artists')],
            artist_id=track_data['artist'].get('id'),
            release_year=track_data.get('streamStartDate')[:4] if track_data.get(
                'streamStartDate') else track_data.get('dateAdded')[:4] if track_data.get('dateAdded') else None,
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            bitrate=bitrate,
            duration=track_data.get('duration'),
            cover_url=cover_url,
            explicit=track_data.get('explicit'),
            tags=self.convert_tags(track_data, album_data, mqa_file),
            codec=track_codec,
            download_extra_kwargs=download_args,
            lyrics_extra_kwargs={'track_data': track_data},
            # check if 'credits' are present (only from get_album_data)
            credits_extra_kwargs={'data': {track_id: track_data['credits']} if 'credits' in track_data else {}},
            additional=self._format_additional_info(track_data)
        )

        if error is not None:
            track_info.error = f'Error: {error}'

        return track_info

    @staticmethod
    def download_temp_header(file_url: str, chunk_size: int = 32768) -> str:
        # create flac temp_location
        temp_location = create_temp_filename() + '.flac'

        # create session and download the file to the temp_location
        r_session = create_requests_session()

        r = r_session.get(file_url, stream=True, verify=False)
        with open(temp_location, 'wb') as f:
            # only download the first chunk_size bytes
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)
                    break

        return temp_location

    @staticmethod
    def parse_mpd(xml: bytes) -> list:
        xml = xml.decode('UTF-8')
        # Removes default namespace definition, don't do that!
        xml = re.sub(r'xmlns="[^"]+"', '', xml, count=1)
        root = ElementTree.fromstring(xml)

        # List of AudioTracks
        tracks = []

        for period in root.findall('Period'):
            for adaptation_set in period.findall('AdaptationSet'):
                for rep in adaptation_set.findall('Representation'):
                    # Check if representation is audio
                    content_type = adaptation_set.get('contentType')
                    if content_type != 'audio':
                        raise ValueError('Only supports audio MPDs!')

                    # Codec checks
                    codec = rep.get('codecs').upper()
                    if codec.startswith('MP4A'):
                        codec = 'AAC'

                    # Segment template
                    seg_template = rep.find('SegmentTemplate')
                    # Add init file to track_urls
                    track_urls = [seg_template.get('initialization')]
                    start_number = int(seg_template.get('startNumber') or 1)

                    # https://dashif-documents.azurewebsites.net/Guidelines-TimingModel/master/Guidelines-TimingModel.html#addressing-explicit
                    # Also see example 9
                    seg_timeline = seg_template.find('SegmentTimeline')
                    if seg_timeline is not None:
                        seg_time_list = []
                        cur_time = 0

                        for s in seg_timeline.findall('S'):
                            # Media segments start time
                            if s.get('t'):
                                cur_time = int(s.get('t'))

                            # Segment reference
                            for i in range((int(s.get('r') or 0) + 1)):
                                seg_time_list.append(cur_time)
                                # Add duration to current time
                                cur_time += int(s.get('d'))

                        # Create list with $Number$ indices
                        seg_num_list = list(range(start_number, len(seg_time_list) + start_number))
                        # Replace $Number$ with all the seg_num_list indices
                        track_urls += [seg_template.get('media').replace('$Number$', str(n)) for n in seg_num_list]

                    tracks.append(AudioTrack(
                        codec=CodecEnum[codec],
                        sample_rate=int(rep.get('audioSamplingRate') or 0),
                        bitrate=int(rep.get('bandwidth') or 0),
                        urls=track_urls
                    ))

        return tracks

    def get_track_download(self, file_url: str = None, audio_track: AudioTrack = None, codec: CodecEnum = None, **kwargs) \
            -> TrackDownloadInfo:
        # only file_url or audio_track at a time

        # Single URL (non-DASH): return URL as-is (no transcode)
        if file_url:
            return TrackDownloadInfo(download_type=DownloadEnum.URL, file_url=file_url)

        # MPEG-DASH
        # use the total_file size for a better progress bar? Is it even possible to calculate the total size from MPD?
        try:
            columns = os.get_terminal_size().columns
            if os.name == 'nt':
                bar = tqdm(audio_track.urls, ncols=(columns - self.oprinter.indent_number),
                           bar_format=' ' * self.oprinter.indent_number + '{l_bar}{bar}{r_bar}')
            else:
                raise OSError
        except OSError:
            bar = tqdm(audio_track.urls, bar_format=' ' * self.oprinter.indent_number + '{l_bar}{bar}{r_bar}')

        # download all segments and save the locations inside temp_locations
        temp_locations = []
        for download_url in bar:
            temp_locations.append(download_to_temp(download_url, extension='mp4'))

        # needed for bar indent
        bar.close()

        # concatenated/Merged .mp4 file
        merged_temp_location = create_temp_filename() + '.mp4'
        output_location = create_temp_filename() + '.' + codec_data[audio_track.codec].container.name

        # download is finished, merge chunks into 1 file
        with open(merged_temp_location, 'wb') as dest_file:
            for temp_location in temp_locations:
                with open(temp_location, 'rb') as segment_file:
                    copyfileobj(segment_file, dest_file)

        # Remux (stream copy) to final container; no transcode.
        try:
            out_kw = dict(acodec='copy', loglevel='error', map_metadata='-1')
            if output_location.lower().endswith(('.m4a', '.mp4')):
                out_kw['movflags'] = '+faststart'
            _get_ffmpeg().input(merged_temp_location, hide_banner=None, y=None).output(
                output_location, **out_kw
            ).run(cmd=_get_ffmpeg_cmd())
            silentremove(merged_temp_location)
            for temp_location in temp_locations:
                silentremove(temp_location)
        except Exception:
            self.print('FFmpeg is not installed or working! Using fallback, may have errors')
            return TrackDownloadInfo(
                download_type=DownloadEnum.TEMP_FILE_PATH,
                temp_file_path=merged_temp_location,
            )

        return TrackDownloadInfo(
            download_type=DownloadEnum.TEMP_FILE_PATH,
            temp_file_path=output_location,
        )

    def get_track_cover(self, track_id: str, cover_options: CoverOptions, data=None) -> CoverInfo:
        if data is None:
            data = {}

        track_data = data[track_id] if track_id in data else self.session.get_track(track_id)
        cover_id = track_data['album'].get('cover')

        if cover_id:
            return CoverInfo(url=self._generate_artwork_url(cover_id, size=cover_options.resolution),
                             file_type=ImageFileTypeEnum.jpg)

        return CoverInfo(url='https://tidal.com/browse/assets/images/defaultImages/defaultTrackImage.png',
                         file_type=ImageFileTypeEnum.png)

    def get_track_lyrics(self, track_id: str, track_data: dict = None) -> LyricsInfo:
        if not track_data:
            track_data = {}

        # get lyrics data for current track id
        lyrics_data = self.session.get_lyrics(track_id)

        if 'error' in lyrics_data and track_data:
            # search for title and artist to find a matching track (non Atmos)
            results = self.search(
                DownloadTypeEnum.track,
                f'{track_data.get("title")} {" ".join(a.get("name") for a in (track_data.get("artists") or []))}',
                limit=10)

            # check every result to find a matching result
            best_tracks = [r.result_id for r in results
                           if r.name == track_data.get('title') and
                           r.artists[0] == track_data.get('artist').get('name') and
                           '◗◖ ATMOS' not in (r.additional or '')]

            # retrieve the lyrics for the first one, otherwise return empty dict
            lyrics_data = self.session.get_lyrics(best_tracks[0]) if len(best_tracks) > 0 else {}

        embedded = lyrics_data.get('lyrics')
        synced = lyrics_data.get('subtitles')

        return LyricsInfo(
            embedded=embedded,
            # regex to remove the space after the timestamp "[mm:ss.xx] " to "[mm:ss.xx]"
            synced=re.sub(r'(\[\d{2}:\d{2}.\d{2,3}])(?: )', r'\1', synced) if synced else None
        )

    @staticmethod
    def _normalize_track_contributors_cache(cached) -> Optional[list]:
        """Return contributor list from data[track_id], or None to fetch from the API."""
        if cached is None:
            return None
        if isinstance(cached, list):
            return cached
        if isinstance(cached, dict):
            if 'credits' in cached:
                credits = cached.get('credits')
                return credits if isinstance(credits, list) else None
            # Full track payloads (e.g. search raw_result) are not contributor caches.
            if any(k in cached for k in ('title', 'album', 'artists', 'duration')):
                return None
        return None

    def get_track_credits(self, track_id: str, data=None) -> Optional[list]:
        if data is None:
            data = {}

        credits_dict = {}

        # Normalize roles to standard tagging keys
        role_mapping = {
            'Lyricist': 'Lyricist',
            'Lyricists': 'Lyricist',
            'Vocals': 'Lyricist',
            'Composer': 'Composer',
            'Composers': 'Composer',
            'Producer': 'Producer',
            'Producers': 'Producer',
            'Music Publisher': 'Music Publisher'
        }

        track_contributors = None
        if track_id in data:
            track_contributors = self._normalize_track_contributors_cache(data[track_id])

        if track_contributors is not None:
            for contributor in track_contributors:
                # Get the contributors list, fallback to empty list if None
                sub_contributors = contributor.get('contributors') or []
                role = contributor.get('type')
                normalized_role = role_mapping.get(role, role)
                
                if normalized_role not in credits_dict:
                    credits_dict[normalized_role] = []
                credits_dict[normalized_role].extend([c.get('name') for c in sub_contributors])
        else:
            contributors_resp = self.session.get_track_contributors(track_id)
            track_contributors = (contributors_resp or {}).get('items') or []

            if track_contributors:
                for contributor in track_contributors:
                    # check if the dict contains no list, create one
                    role = contributor.get('role')
                    if role:
                        normalized_role = role_mapping.get(role, role)
                        if normalized_role not in credits_dict:
                            credits_dict[normalized_role] = []

                        credits_dict[normalized_role].append(contributor.get('name'))

        if len(credits_dict) > 0:
            # convert the dictionary back to a list of CreditsInfo
            return [CreditsInfo(sanitise_name(k), v) for k, v in credits_dict.items()]
        return None


    @staticmethod
    def _extract_label_from_credits(credits_list: list) -> Optional[str]:
        if not credits_list:
            return None

        labels = []
        for credit in credits_list:
            # Check for various forms of label/publisher in credits
            # Raw API uses 'role' or 'type' depending on endpoint
            credit_type = credit.get('type') or credit.get('role')
            if credit_type in {'Record Label', 'Label', 'Production'}:
                # Contributors can be a list or we use the name field directly if it's a flattened credit
                contributors = credit.get('contributors')
                if contributors:
                    for c in contributors:
                        if c.get('name') and c.get('name') not in labels:
                            labels.append(c.get('name'))
                elif credit.get('name'):
                    if credit.get('name') not in labels:
                        labels.append(credit.get('name'))

        return '; '.join(labels) if labels else None

    @staticmethod
    def _extract_label_from_copyright(copyright_str: str) -> Optional[str]:
        if not copyright_str:
            return None

        # Remove (C), (P), ©, ℗ and leading years
        # Example: "© 2022 Taylor Swift" -> "Taylor Swift"
        import re
        # Strip leading symbols and years
        res = re.sub(r'^[©℗(C)(P)\s\d-]{2,}', '', copyright_str).strip()
        return res if res else None

    @staticmethod
    def convert_tags(track_data: dict, album_data: dict, mqa_file: MqaIdentifier = None) -> Tags:
        track_name = track_data.get('title')
        track_name += f' ({track_data.get("version")})' if track_data.get('version') else ''

        extra_tags = {}
        if mqa_file is not None:
            encoder_time = datetime.now().strftime("%b %d %Y %H:%M:%S")
            extra_tags = {
                'ENCODER': f'MQAEncode v1.1, 2.4.0+0 (278f5dd), E24F1DE5-32F1-4930-8197-24954EB9D6F4, {encoder_time}',
                'MQAENCODER': f'MQAEncode v1.1, 2.4.0+0 (278f5dd), E24F1DE5-32F1-4930-8197-24954EB9D6F4, {encoder_time}',
                'ORIGINALSAMPLERATE': str(mqa_file.original_sample_rate)
            }

        album_artist = get_primary_artist(album_data.get('artist', {}).get('name')) if 'artist' in album_data else None

        # Extract label from credits or fallback to copyright
        credits_list = track_data.get('credits')
        label = ModuleInterface._extract_label_from_credits(credits_list)
        if not label:
            # Fallback to copyright string (cleaned up)
            label = ModuleInterface._extract_label_from_copyright(track_data.get('copyright'))

        return Tags(
            album_artist=album_artist,
            track_number=track_data.get('trackNumber'),
            total_tracks=album_data.get('numberOfTracks'),
            disc_number=track_data.get('volumeNumber'),
            total_discs=album_data.get('numberOfVolumes'),
            isrc=track_data.get('isrc'),
            upc=album_data.get('upc'),
            release_date=album_data.get('releaseDate'),
            copyright=track_data.get('copyright'),
            label=label,
            replay_gain=track_data.get('replayGain'),
            replay_peak=track_data.get('peak'),
            track_url=f"https://listen.tidal.com/track/{track_data.get('id')}",
            extra_tags=extra_tags
        )

    def _ensure_guest_session(self):
        """Ensure a guest (client_credentials) session exists for preview playback.
        Creates one on-the-fly if not already present."""
        guest_key = SessionType.GUEST.name
        if guest_key in self.session.sessions:
            guest = self.session.sessions[guest_key]
            # Refresh if expired
            if hasattr(guest, 'expires') and guest.expires and datetime.now() > guest.expires:
                try:
                    guest.refresh()
                except Exception:
                    pass
            return guest
        # No guest session exists — create one
        try:
            guest = TidalGuestSession(
                self.settings.get('guest_token', 'txNoH4kkV41MfH25'),
                self.settings.get('guest_secret', 'dQjy0MinCEvxi1O4UmxvxWnDjt4cgHBPw8ll6nYBk98=')
            )
            guest.auth()
            self.session.sessions[guest_key] = guest
            return guest
        except Exception as e:
            logging.debug(f'{module_information.service_name}: Could not create guest session for preview: {e}')
            return None

    def get_preview_stream_url(self, track_id: str):
        """Fetch a 30-second preview stream URL for a track.
        Works without user login by using a guest (client_credentials) session.
        Tries v1 PREVIEW API first, then falls back to v2 OpenAPI (web player API).
        Returns a playable audio URL/path or None."""
        guest = self._ensure_guest_session()
        if not guest:
            logging.debug(f'{module_information.service_name}: No guest session available for preview')
            return None

        # --- Attempt 1: v1 API with PREVIEW assetpresentation ---
        try:
            preview_data = self.session.get_track_preview_url(track_id, session_override=guest)
            result = self._extract_preview_from_manifest(preview_data, track_id)
            if result:
                return result
        except Exception as e:
            logging.debug(f'{module_information.service_name}: v1 preview failed for track {track_id}: {e}')

        # --- Attempt 2: v2 OpenAPI (same as the Tidal web player) ---
        try:
            preview_data = self.session.get_track_preview_v2(track_id, session_override=guest)
            result = self._extract_preview_from_manifest(preview_data, track_id)
            if result:
                return result
        except Exception as e:
            logging.debug(f'{module_information.service_name}: v2 preview failed for track {track_id}: {e}')

        return None

    def _extract_preview_from_manifest(self, preview_data, track_id):
        """Extract a playable preview from manifest data (shared by v1 and v2 paths).
        For DASH manifests, downloads and concatenates segments into a temp .m4a file.
        For BTS/JSON manifests, returns the direct URL.
        Returns a file path or URL, or None."""
        if not preview_data:
            return None

        manifest_mime = preview_data.get('manifestMimeType', '')
        manifest_b64 = preview_data.get('manifest', '')
        if not manifest_b64:
            return None

        try:
            if 'application/dash+xml' in manifest_mime:
                # DASH manifest — download segments and concatenate
                manifest_bytes = base64.b64decode(manifest_b64)
                audio_tracks = self.parse_mpd(manifest_bytes)
                if not audio_tracks or not audio_tracks[0].urls:
                    return None
                urls = audio_tracks[0].urls
                # Download init + all media segments and concatenate into one .m4a
                segment_paths = []
                try:
                    for url in urls:
                        seg_path = download_to_temp(url, extension='mp4')
                        segment_paths.append(seg_path)
                    preview_path = os.path.join(tempfile.gettempdir(), f'orpheus_tidal_preview_{track_id}.m4a')
                    with open(preview_path, 'wb') as out:
                        for p in segment_paths:
                            with open(p, 'rb') as f:
                                out.write(f.read())
                finally:
                    for p in segment_paths:
                        silentremove(p)
                if preview_path and os.path.exists(preview_path) and os.path.getsize(preview_path) > 0:
                    return preview_path
                silentremove(preview_path)
                return None

            elif 'application/vnd.tidal.bts' in manifest_mime:
                # BTS (direct URL) manifest
                manifest_json = json.loads(base64.b64decode(manifest_b64).decode('utf-8'))
                urls = manifest_json.get('urls', [])
                if urls:
                    return urls[0]

            else:
                # Unknown manifest type — try JSON parsing as fallback
                manifest_json = json.loads(base64.b64decode(manifest_b64).decode('utf-8'))
                urls = manifest_json.get('urls', [])
                if urls:
                    return urls[0]

        except Exception as e:
            logging.debug(f'{module_information.service_name}: Error extracting preview manifest for track {track_id}: {e}')

        return None

    def _format_additional_info(self, item):
        """Format additional info (traits) for display in the GUI."""
        additional = []

        # Track count for albums
        track_count = item.get('numberOfTracks') or item.get('number_of_tracks')
        if track_count:
            additional.append(f"1 track" if track_count == 1 else f"{track_count} tracks")

        tags = item.get('mediaMetadata', {}).get('tags', [])
        
        if 'DOLBY_ATMOS' in tags or 'DOLBY_ATMOS' in item.get('audioModes', []):
            additional.append("◗◖ ATMOS")
        if 'SONY_360RA' in tags or 'SONY_360RA' in item.get('audioModes', []):
            additional.append("360 Reality Audio")
        if 'HIRES_LOSSLESS' in tags or item.get('audioQuality') == 'HI_RES':
            additional.append("🅷 HI-RES")
        elif (
            item.get('audioQuality') in ('LOSSLESS', 'LOSSLESS_STEREO')
            or 'LOSSLESS' in tags
        ) and not any('HI-RES' in str(x) or '🅷' in str(x) for x in additional):
            additional.append('FLAC')

        return " / ".join(additional) if additional else None
