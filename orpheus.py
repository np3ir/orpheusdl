#!/usr/bin/env python3
"""
OrpheusDL CLI - Command-line interface for music downloading
Supports searching, downloading, and module management
"""
import os
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import re
import json
import argparse
import traceback
from urllib.parse import urlparse

# 1. Environment setup must happen before other imports
os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION'] = 'python'

from utils.vendor_bootstrap import bootstrap_vendor_paths

bootstrap_vendor_paths()

from orpheus.core import *
from orpheus.music_downloader import beauty_format_seconds

try:
    from modules.spotify.spotify_api import SpotifyAuthError, SpotifyRateLimitDetectedError
except ModuleNotFoundError:
    SpotifyAuthError = None  # type: ignore
    SpotifyRateLimitDetectedError = None  # type: ignore

# ============================================================================
# GLOBAL PATTERNS CACHE - Pre-compiled for performance
# ============================================================================

_compiled_patterns = {}


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def sanitize_args(args_list):
    """
    Intercepts and cleans specific URLs (like Tidal) in the arguments list
    before the main parser processes them.
    """
    cleaned_args = []
    # Preserve the script name (args_list[0]) if present
    if args_list:
        cleaned_args.append(args_list[0])

    for arg in args_list[1:]:
        new_arg = arg

        # --- UNIVERSAL TIDAL LINK CLEANER ---
        # Detects any Tidal URL containing /track/ID and strips everything else.
        # This handles:
        # 1. mixed links: tidal.com/album/ID/track/ID_TRACK -> tidal.com/track/ID_TRACK
        # 2. trailing junk: tidal.com/track/ID/u -> tidal.com/track/ID
        if 'tidal.com' in arg and '/track/' in arg:
            match = re.search(r'/track/(\d+)', arg)
            if match:
                clean_id = match.group(1)
                reconstructed_url = f'https://tidal.com/track/{clean_id}'

                # Only update and print if the URL actually changed
                if reconstructed_url != arg:
                    print(f'\n✨ Tidal Link automatically cleaned: {reconstructed_url}\n')
                    new_arg = reconstructed_url

        cleaned_args.append(new_arg)

    return cleaned_args


def setup_ffmpeg_path():
    """Setup FFmpeg path from settings.json to match GUI behavior"""
    try:
        # Construct absolute path to config file based on this script's location
        # This prevents errors if running the script from a different directory
        base_dir = os.path.dirname(os.path.abspath(__file__))
        settings_path = os.path.join(base_dir, "config", "settings.json")

        if os.path.exists(settings_path):
            try:
                with open(settings_path, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not read settings.json: {e}")
                return

            # Get FFmpeg path setting
            ffmpeg_path_setting = settings.get("global", {}).get("advanced", {}).get("ffmpeg_path", "ffmpeg")

            if isinstance(ffmpeg_path_setting, str):
                ffmpeg_path_setting = ffmpeg_path_setting.strip()

                # If it's a custom path (not just "ffmpeg"), add directory to PATH
                if ffmpeg_path_setting and ffmpeg_path_setting.lower() != "ffmpeg":
                    if os.path.isfile(ffmpeg_path_setting):
                        ffmpeg_dir = os.path.dirname(ffmpeg_path_setting)
                        if ffmpeg_dir:
                            current_path = os.environ.get("PATH", "")
                            if ffmpeg_dir not in current_path.split(os.pathsep):
                                os.environ["PATH"] = ffmpeg_dir + os.pathsep + current_path
        else:
            pass  # Settings file not found, using defaults
    except Exception as e:
        # Don't fail if we can't setup FFmpeg path, just continue
        print(f"Warning: Could not setup FFmpeg path: {e}")


def load_urls_from_file(file_path):
    """
    Load URLs from a text file.
    Each line is treated as a separate URL.
    Empty lines and comments (starting with #) are ignored.

    Args:
        file_path: Path to the file containing URLs

    Returns:
        Tuple of cleaned URLs

    Raises:
        IOError: If file cannot be read
        UnicodeDecodeError: If file is not UTF-8 encoded
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            arguments = tuple(
                line.strip()
                for line in f
                if line.strip() and not line.startswith('#')
            )
        return arguments
    except IOError as e:
        print(f"Error reading file '{file_path}': {e}")
        exit(1)
    except UnicodeDecodeError as e:
        print(f"File encoding error in '{file_path}'. Please use UTF-8 encoding: {e}")
        exit(1)


def init_compiled_patterns(orpheus):
    """
    Pre-compile all netloc regex patterns for better performance.
    Avoids recompiling patterns in loops.
    """
    global _compiled_patterns
    if not _compiled_patterns:
        _compiled_patterns = {
            pattern: re.compile(pattern)
            for pattern in orpheus.module_netloc_constants.keys()
        }


def validate_arguments(mode, args_list):
    """
    Validate that sufficient arguments are provided for each mode.

    Args:
        mode: The operation mode (search, download, settings, etc.)
        args_list: List of arguments provided

    Returns:
        True if valid, False otherwise
    """
    required_args = {
        'search': 4,
        'luckysearch': 4,
        'download': 4,
        'settings': 2,
        'sessions': 3
    }

    if mode in required_args:
        if len(args_list) < required_args[mode]:
            print(f"Error: '{mode}' requires at least {required_args[mode]} arguments")
            return False

    return True


# ============================================================================
# MAIN FUNCTION
# ============================================================================

def main():
    # 1. Clean arguments immediately
    sys.argv = sanitize_args(sys.argv)

    # 2. Setup FFmpeg
    setup_ffmpeg_path()

    print(r'''
   ____             _                    _____  _      
  / __ \           | |                  |  __ \| |     
 | |  | |_ __ _ __ | |__   ___ _   _ ___| |  | | |     
 | |  | | '__| '_ \| '_ \ / _ \ | | / __| |  | | |     
 | |__| | |  | |_) | | | |  __/ |_| \__ \ |__| | |____ 
  \____/|_|  | .__/|_| |_|\___|\__,_|___/_____/|______|
             | |                                       
             |_|                                       

            ''')

    help_text = (
        'Use "settings [option]" for orpheus controls (coreupdate, fullupdate, modinstall), '
        '"settings [module][option]" for module specific options (update, test, setup), '
        'searching by "[search/luckysearch] [module][track/artist/playlist/album] [query]", '
        'or just putting in URLs (wrap in double quotes if needed)'
    )

    parser = argparse.ArgumentParser(description='Orpheus: modular music archival')
    parser.add_argument('-p', '--private', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-o', '--output',
                        help='Select a download output path. Default is the provided download path in config/settings.json')
    parser.add_argument('-lr', '--lyrics', default='default', help='Set module to get lyrics from')
    parser.add_argument('-cv', '--covers', default='default', help='Override module to get covers from')
    parser.add_argument('-cr', '--credits', default='default', help='Override module to get credits from')
    parser.add_argument('-sd', '--separatedownload', default='default',
                        help='Select a different module that will download the playlist instead of the main module. Only for playlists.')
    parser.add_argument('arguments', nargs='*', help=help_text)
    args = parser.parse_args()

    orpheus = Orpheus(args.private)

    # Set global progress bar setting for the CLI
    from utils.utils import set_progress_bars_enabled
    progress_bar_setting = orpheus.settings.get('global', {}).get('general', {}).get('progress_bar', False)
    set_progress_bars_enabled(progress_bar_setting)

    # Pre-compile regex patterns for performance
    init_compiled_patterns(orpheus)

    if not args.arguments:
        parser.print_help()
        exit()

    orpheus_mode = args.arguments[0].lower()

    # ========================================================================
    # MODE HANDLERS
    # ========================================================================

    if orpheus_mode == 'settings':
        # FIXED: Validate arguments
        if not validate_arguments('settings', args.arguments):
            exit(1)

        setting = args.arguments[1].lower()
        if setting == 'refresh':
            print('settings.json has been refreshed successfully.')
            return
        elif setting == 'core_update':
            print("❌ Error: 'core_update' feature is not yet implemented")
            exit(1)
        elif setting == 'full_update':
            print("❌ Error: 'full_update' feature is not yet implemented")
            exit(1)
        elif setting == 'module_install':
            print("❌ Error: 'module_install' feature is not yet implemented")
            exit(1)
        elif setting == 'test_modules':
            print("❌ Error: 'test_modules' feature is not yet implemented")
            exit(1)
        elif setting in orpheus.module_list:
            orpheus.load_module(setting)
            if len(args.arguments) < 3:
                print(f"Error: Module '{setting}' requires a sub-option (update, setup, test, adjust_setting)")
                exit(1)
            modulesetting = args.arguments[2].lower()
            if modulesetting == 'update':
                print(f"❌ Error: 'update' for module '{setting}' is not yet implemented")
                exit(1)
            elif modulesetting == 'setup':
                print(f"❌ Error: 'setup' for module '{setting}' is not yet implemented")
                exit(1)
            elif modulesetting == 'adjust_setting':
                print(f"❌ Error: 'adjust_setting' for module '{setting}' is not yet implemented")
                exit(1)
            elif modulesetting == 'test':
                print(f"❌ Error: 'test' for module '{setting}' is not yet implemented")
                exit(1)
            else:
                raise Exception(f'Unknown setting "{modulesetting}" for module "{setting}"')
        else:
            raise Exception(f'Unknown setting: "{setting}"')

    elif orpheus_mode == 'sessions':
        # FIXED: Validate arguments
        if not validate_arguments('sessions', args.arguments):
            exit(1)

        module = args.arguments[1].lower()
        if module in orpheus.module_list:
            option = args.arguments[2].lower()
            if option == 'add':
                print(f"❌ Error: 'add' session for module '{module}' is not yet implemented")
                exit(1)
            elif option == 'delete':
                print(f"❌ Error: 'delete' session for module '{module}' is not yet implemented")
                exit(1)
            elif option == 'list':
                print(f"❌ Error: 'list' sessions for module '{module}' is not yet implemented")
                exit(1)
            elif option == 'test':
                if len(args.arguments) < 4:
                    print(f"Error: 'test' requires a session name (or 'all')")
                    exit(1)
                session_name = args.arguments[3].lower()
                if session_name == 'all':
                    print(f"❌ Error: 'test all' sessions for module '{module}' is not yet implemented")
                    exit(1)
                else:
                    print(f"❌ Error: 'test' session '{session_name}' for module '{module}' is not yet implemented")
                    exit(1)
            else:
                raise Exception(f'Unknown option "{option}", choose add/delete/list/test')
        else:
            raise Exception(f'Unknown module "{module}"')

    else:
        # ====================================================================
        # DOWNLOAD / SEARCH LOGIC
        # ====================================================================

        path = args.output if args.output else orpheus.settings['global']['general']['download_path']
        if path.endswith('/'):
            path = path.rstrip('/')
        os.makedirs(path, exist_ok=True)

        media_types = '/'.join(i.name for i in DownloadTypeEnum)

        # FIXED: Initialize early to avoid NameError
        media_to_download = {}

        if orpheus_mode == 'search' or orpheus_mode == 'luckysearch':
            # FIXED: Validate arguments
            if not validate_arguments(orpheus_mode, args.arguments):
                exit(1)

            if len(args.arguments) > 3:
                modulename = args.arguments[1].lower()
                if modulename in orpheus.module_list:
                    try:
                        query_type = DownloadTypeEnum[args.arguments[2].lower()]
                    except KeyError:
                        raise Exception(f'{args.arguments[2].lower()} is not a valid search type! Choose {media_types}')
                    lucky_mode = True if orpheus_mode == 'luckysearch' else False

                    query = ' '.join(args.arguments[3:])
                    module = orpheus.load_module(modulename)
                    print("Searching... Please wait.")
                    items = module.search(query_type, query, limit=(
                        1 if lucky_mode else orpheus.settings['global']['general']['search_limit']))
                    if len(items) == 0:
                        raise Exception(f'No search results for {query_type.name}: {query}')

                    if lucky_mode:
                        selection = 0
                    else:
                        for index, item in enumerate(items, start=1):
                            additional_details = '[E] ' if item.explicit else ''
                            additional_details += f'[{beauty_format_seconds(item.duration)}] ' if item.duration else ''
                            additional_details += f'[{item.year}] ' if item.year else ''
                            additional_details += ' '.join(
                                [f'[{i}]' for i in item.additional]) if item.additional else ''
                            if query_type is not DownloadTypeEnum.artist:
                                # FIXED: Use isinstance instead of is
                                artists = ', '.join(item.artists) if isinstance(item.artists, list) else item.artists
                                print(f'{str(index)}. {item.name} - {artists} {additional_details}')
                            else:
                                print(f'{str(index)}. {item.name} {additional_details}')

                        selection_input = input('Selection: ').strip('\r\n ')
                        if selection_input.lower() in ['e', 'q', 'x', 'exit', 'quit']:
                            exit(0)
                        if not selection_input.isdigit():
                            raise Exception('Input a number')
                        selection = int(selection_input) - 1
                        if selection < 0 or selection >= len(items):
                            raise Exception('Invalid selection')
                        print()

                    selected_item = items[selection]
                    media_to_download = {modulename: [
                        MediaIdentification(media_type=query_type, media_id=selected_item.result_id,
                                            extra_kwargs=selected_item.extra_kwargs or {})]}
                elif modulename == 'multi':
                    print("❌ Error: 'multi' module search is not yet implemented")
                    exit(1)
                else:
                    modules = [i for i in orpheus.module_list if
                               ModuleFlags.hidden not in orpheus.module_settings[i].flags]
                    raise Exception(
                        f'Unknown module name "{modulename}". Must select from: {", ".join(modules)}')
            else:
                print(f'Search must be done as orpheus.py [search/luckysearch] [module] [{media_types}] [query]')
                exit(1)

        elif orpheus_mode == 'download':
            # FIXED: Validate arguments
            if not validate_arguments('download', args.arguments):
                exit(1)

            if len(args.arguments) > 3:
                modulename = args.arguments[1].lower()
                if modulename in orpheus.module_list:
                    try:
                        media_type = DownloadTypeEnum[args.arguments[2].lower()]
                    except KeyError:
                        raise Exception(
                            f'{args.arguments[2].lower()} is not a valid download type! Choose {media_types}')
                    media_to_download = {modulename: [MediaIdentification(media_type=media_type, media_id=i) for i in
                                                      args.arguments[3:]]}
                else:
                    modules = [i for i in orpheus.module_list if
                               ModuleFlags.hidden not in orpheus.module_settings[i].flags]
                    raise Exception(
                        f'Unknown module name "{modulename}". Must select from: {", ".join(modules)}')
            else:
                print(
                    f'Download must be done as orpheus.py [download] [module] [{media_types}] [media ID 1] [media ID 2] ...')
                exit(1)

        else:  # Automatic URL detection
            # FIXED: Better file handling with context manager
            if len(args.arguments) == 1 and os.path.isfile(args.arguments[0]):
                arguments = load_urls_from_file(args.arguments[0])
            else:
                arguments = args.arguments

            for link in arguments:
                link = link.strip()

                # Remove trailing slash (cleaner way)
                link = link.rstrip('/')

                if not link:
                    continue

                if link.startswith('http'):
                    url = urlparse(link)
                    components = url.path.split('/')

                    # FIXED: Use pre-compiled patterns for better performance
                    service_name = None
                    for pattern, module_name in orpheus.module_netloc_constants.items():
                        if _compiled_patterns[pattern].search(url.netloc):
                            service_name = module_name
                            break

                    if not service_name:
                        raise Exception(f'URL location "{url.netloc}" is not found in modules!')
                    if service_name not in media_to_download:
                        media_to_download[service_name] = []

                    if orpheus.module_settings[service_name].url_decoding is ManualEnum.manual:
                        module = orpheus.load_module(service_name)
                        media_to_download[service_name].append(module.custom_url_parse(link))
                    else:
                        if not components or len(components) <= 2:
                            print(f'\tInvalid URL: "{link}"')
                            exit(1)

                        url_constants = orpheus.module_settings[service_name].url_constants
                        if not url_constants:
                            url_constants = {
                                'track': DownloadTypeEnum.track,
                                'album': DownloadTypeEnum.album,
                                'playlist': DownloadTypeEnum.playlist,
                                'artist': DownloadTypeEnum.artist
                            }

                        type_matches = [media_type for url_check, media_type in url_constants.items() if
                                        url_check in components]

                        if not type_matches:
                            print(f'Invalid URL: "{link}"')
                            exit(1)

                        media_to_download[service_name].append(
                            MediaIdentification(media_type=type_matches[-1], media_id=components[-1]))
                else:
                    raise Exception(f'Invalid argument: "{link}"')

        # Third-party module setup
        tpm = {ModuleModes.covers: '', ModuleModes.lyrics: '', ModuleModes.credits: ''}
        for i in tpm:
            moduleselected = getattr(args, i.name).lower()
            if moduleselected == 'default':
                moduleselected = orpheus.settings['global']['module_defaults'][i.name]
            if moduleselected == 'default':
                moduleselected = None
            tpm[i] = moduleselected
        sdm = args.separatedownload.lower()

        # FIXED: Check if media_to_download is empty and exit
        if not media_to_download:
            print('No links given')
            exit(0)

        # Beatport quality override
        original_quality = None
        beatport_quality_override = False
        if 'beatport' in media_to_download and orpheus.settings['global']['general']['download_quality'] in ['high',
                                                                                                             'low']:
            original_quality = orpheus.settings['global']['general']['download_quality']
            orpheus.settings['global']['general']['download_quality'] = 'lossless'
            beatport_quality_override = True
            print(f'Beatport: Automatically switching from "{original_quality}" to "lossless" quality')

        try:
            orpheus_core_download(orpheus, media_to_download, tpm, sdm, path)
        finally:
            if beatport_quality_override and original_quality:
                orpheus.settings['global']['general']['download_quality'] = original_quality


# ============================================================================
# SCRIPT ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print('\n\t^C pressed - aborting')
        exit(0)
    except Exception as e:
        if SpotifyAuthError is not None and isinstance(e, SpotifyAuthError):
            print(f'\nSpotify Authentication Error: {e}')
            print('Please try the command again. If the issue persists, check your Spotify credentials.')
            exit(1)

        print("\nAn unexpected error occurred:")
        traceback.print_exc()
        exit(1)