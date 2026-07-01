import pickle, requests, errno, hashlib, math, os, re, operator, asyncio
import aiohttp
import aiofiles
import platform  # <--- CORRECCIÓN DE IMPORTACIÓN
from tqdm import tqdm as original_tqdm
import threading
from urllib3.util.retry import Retry
from functools import reduce
from PIL import Image, ImageChops
from requests.adapters import HTTPAdapter
import urllib.parse  # <--- NECESARIO PARA LA FUNCIÓN _is_valid_url

# Global flag for progress bar settings (more reliable than thread-local in async contexts)
_progress_bars_enabled = True
_progress_bars_lock = threading.Lock()


def tqdm(*args, **kwargs):
    """Custom tqdm wrapper that respects global progress bar settings"""
    # Check if progress bars are globally disabled
    global _progress_bars_enabled
    with _progress_bars_lock:
        if not _progress_bars_enabled:
            kwargs['disable'] = True
    return original_tqdm(*args, **kwargs)


def set_progress_bars_enabled(enabled):
    """Set whether progress bars should be enabled globally"""
    global _progress_bars_enabled
    with _progress_bars_lock:
        _progress_bars_enabled = enabled


from PIL import Image, ImageChops
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from functools import reduce


def hash_string(input_str: str, hash_type: str = 'MD5'):
    if hash_type == 'MD5':
        return hashlib.md5(input_str.encode("utf-8")).hexdigest()
    else:
        raise Exception('Invalid hash type selected')


def create_requests_session():
    session_ = requests.Session()
    retries = Retry(total=10, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
    session_.mount('http://', HTTPAdapter(max_retries=retries))
    session_.mount('https://', HTTPAdapter(max_retries=retries))
    return session_


def create_aiohttp_session():
    """Create an aiohttp session with retry and timeout configuration"""
    timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_read=60)

    # Optimized connector settings for better concurrent performance
    connector = aiohttp.TCPConnector(
        limit=200,  # Increased total connection pool from 100 to 200
        limit_per_host=50,  # Increased per-host connections from 30 to 50
        enable_cleanup_closed=True,
        use_dns_cache=False  # Disable DNS cache to avoid aiodns issues on Windows
    )

    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers={'User-Agent': 'OrpheusDL/1.0'},
        trust_env=True
    )


sanitise_name = lambda name: re.sub(r'[:]', ' - ', re.sub(r'[\\/*?"<>|$]', '', re.sub(r'[\x00-\x1F\x7F]', '',
                                                                                      str(name).strip()))) if name else ''


# --- INICIO DE FUNCIÓN QUE ARREGLA EL SLASH FINAL ---
def _is_valid_url(url: str, type: str) -> bool:
    import re

    # 1. Limpieza de la URL para eliminar parámetros de consulta y fragmentos (ej: ?utm_source=...)
    parsed_url = urllib.parse.urlparse(url)
    url = urllib.parse.urlunparse(parsed_url._replace(query='', fragment=''))

    # 2. Definición de la expresión regular
    # Utilizamos /?$ al final para que el slash sea opcional.
    regex = ''
    if type == 'album':
        regex = r'https?:\/\/(?:www\.|listen\.|m\.)?(?:tidal|spotify|deezer|qobuz|applemusic|bandcamp)\.com\/album\/[0-9a-zA-Z\/-]+\/?$'
    elif type == 'track':
        regex = r'https?:\/\/(?:www\.|listen\.|m\.)?(?:tidal|spotify|deezer|qobuz|applemusic|bandcamp)\.com\/track\/[0-9a-zA-Z\/-]+\/?$'
    elif type == 'playlist':
        regex = r'https?:\/\/(?:www\.|listen\.|m\.)?(?:tidal|spotify|deezer|qobuz|applemusic|bandcamp)\.com\/playlist\/[0-9a-zA-Z\/-]+\/?$'
    elif type == 'artist':
        regex = r'https?:\/\/(?:www\.|listen\.|m\.)?(?:tidal|spotify|deezer|qobuz|applemusic|bandcamp)\.com\/artist\/[0-9a-zA-Z\/-]+\/?$'

    if not regex:
        return False

    return bool(re.match(regex, url, re.IGNORECASE))


# --- FIN DE FUNCIÓN QUE ARREGLA EL SLASH FINAL ---


def fix_byte_limit(path: str) -> str:
    # Max bytes allowed for the path after converting to UTF-8 (250 bytes for safety)
    MAX_PATH_BYTES = 250

    if platform.system() != 'Windows':
        return path

    path_bytes = path.encode('utf-8')

    if len(path_bytes) > MAX_PATH_BYTES:
        import os

        # FIX: Evitar el error de ruta C: vs Z:

        # 1. Get directory and base name
        dirname = os.path.dirname(path)
        basename = os.path.basename(path)

        # 2. Convert the base name to bytes
        basename_bytes = basename.encode('utf-8')

        # 3. Calculate available bytes for the filename
        # -1 accounts for the directory separator (slash/backslash)
        available_bytes = MAX_PATH_BYTES - len(dirname.encode('utf-8')) - 1

        if available_bytes < 1:
            # If the directory path itself is too long, we cannot truncate the filename
            return path

            # 4. Truncate the base name if necessary
        truncated_basename = basename
        if len(basename_bytes) > available_bytes:
            # Truncate to the maximum allowed byte length
            truncated_basename = basename_bytes[:available_bytes].decode('utf-8', 'ignore')

        # Reconstruct the path using the truncated basename
        path = os.path.join(dirname, truncated_basename).replace('\\', '/')

    return path


r_session = create_requests_session()


# ... (El resto de las funciones como download_file_async, etc., deberían seguir abajo)

async def download_file_async(session, url, file_location, headers={}, enable_progress_bar=False, indent_level=0, artwork_settings=None, max_retries=3):
    """Async version of download_file using aiohttp - returns (file_location, bytes_downloaded)"""
    if os.path.isfile(file_location):
        # File already exists - return 0 bytes downloaded
        return (file_location, 0)

    # Create directory structure if it doesn't exist
    directory = os.path.dirname(file_location)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    bytes_downloaded = 0

    for attempt in range(max_retries):
        try:
            async with session.get(url, headers=headers, ssl=False) as response:
                response.raise_for_status()
                
                total = None
                if 'content-length' in response.headers:
                    total = int(response.headers['content-length'])

                # Use aiofiles for async file writing
                async with aiofiles.open(file_location, 'wb') as f:
                    if enable_progress_bar and total:
                        # Create indented progress bar with proper formatting
                        import sys
                        from io import StringIO
                        
                        class IndentedOutput:
                            def __init__(self, indent_level):
                                self.indent_level = indent_level
                                
                            def write(self, text):
                                # Add indentation to each line
                                lines = text.split('\n')
                                indented_lines = []
                                for line in lines:
                                    if line.strip():  # Only indent non-empty lines
                                        indented_lines.append(' ' * self.indent_level + line)
                                    else:
                                        indented_lines.append(line)
                                sys.stdout.write('\n'.join(indented_lines))
                                
                            def flush(self):
                                sys.stdout.flush()
                        
                        bar = tqdm(
                            total=total, 
                            unit='B', 
                            unit_scale=True, 
                            unit_divisor=1024, 
                            initial=0, 
                            miniters=1,
                            leave=False,
                            file=IndentedOutput(indent_level)
                        )
                        
                        async for chunk in response.content.iter_chunked(8192):
                            await f.write(chunk)
                            bar.update(len(chunk))
                            bytes_downloaded += len(chunk)
                        bar.close()
                    else:
                        async for chunk in response.content.iter_chunked(8192):
                            await f.write(chunk)
                            bytes_downloaded += len(chunk)

                # Handle artwork resizing if needed
                if artwork_settings and artwork_settings.get('should_resize', False):
                    new_resolution = artwork_settings.get('resolution', 1400)
                    new_format = artwork_settings.get('format', 'jpeg')
                    if new_format == 'jpg': new_format = 'jpeg'
                    new_compression = artwork_settings.get('compression', 'low')
                    if new_compression == 'low':
                        new_compression = 90
                    elif new_compression == 'high':
                        new_compression = 70
                    if new_format == 'png': new_compression = None
                    with Image.open(file_location) as im:
                        im = im.resize((new_resolution, new_resolution), Image.Resampling.BICUBIC)
                        im.save(file_location, new_format, quality=new_compression)
                
                return (file_location, bytes_downloaded)
                
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if isinstance(e, aiohttp.ClientResponseError) and e.status == 429:
                # Special handling for Rate Limiting
                retry_after = 20
                # Try to get Retry-After header from response if possible, 
                # but ClientResponseError doesn't always carry headers easily unless we passed them.
                # Just wait 10s and retry without counting towards limit? 
                # For now, just wait longer and continue.
                print(f"Rate limited (429). Waiting {retry_after}s...")
                await asyncio.sleep(retry_after)
                # Decrement attempt to not count this as a failure? 
                # Cannot modify loop variable 'attempt'.
                # But we can just continue. If we want infinite retries for 429, we need a while loop.
                # For now, let's just accept the wait.
                
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
                continue
            else:
                # Clean up partial file on final failure
                if os.path.isfile(file_location):
                    try:
                        os.remove(file_location)
                    except:
                        pass
                raise e
        except KeyboardInterrupt:
            if os.path.isfile(file_location):
                print(f'\tDeleting partially downloaded file "{str(file_location)}"')
                silentremove(file_location)
            raise KeyboardInterrupt

def download_file(url, file_location, headers={}, enable_progress_bar=False, indent_level=0, artwork_settings=None):
    """Synchronous wrapper for the async download function for backward compatibility"""
    if os.path.isfile(file_location):
        return None

    # Create directory structure if it doesn't exist
    directory = os.path.dirname(file_location)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    r = r_session.get(url, stream=True, headers=headers, verify=False)

    total = None
    if 'content-length' in r.headers:
        total = int(r.headers['content-length'])

    try:
        with open(file_location, 'wb') as f:
            if enable_progress_bar and total:
                # Create indented progress bar with proper formatting
                import sys
                from io import StringIO
                
                class IndentedOutput:
                    def __init__(self, indent_level):
                        self.indent_level = indent_level
                        
                    def write(self, text):
                        # Add indentation to each line
                        lines = text.split('\n')
                        indented_lines = []
                        for line in lines:
                            if line.strip():  # Only indent non-empty lines
                                indented_lines.append(' ' * self.indent_level + line)
                            else:
                                indented_lines.append(line)
                        sys.stdout.write('\n'.join(indented_lines))
                        
                    def flush(self):
                        sys.stdout.flush()
                
                bar = tqdm(
                    total=total, 
                    unit='B', 
                    unit_scale=True, 
                    unit_divisor=1024, 
                    initial=0, 
                    miniters=1,
                    leave=False,
                    file=IndentedOutput(indent_level)
                )
                for chunk in r.iter_content(chunk_size=1024):
                    if chunk:  # filter out keep-alive new chunks
                        f.write(chunk)
                        bar.update(len(chunk))
                bar.close()
            else:
                [f.write(chunk) for chunk in r.iter_content(chunk_size=1024) if chunk]
        if artwork_settings and artwork_settings.get('should_resize', False):
            new_resolution = artwork_settings.get('resolution', 1400)
            new_format = artwork_settings.get('format', 'jpeg')
            if new_format == 'jpg': new_format = 'jpeg'
            new_compression = artwork_settings.get('compression', 'low')
            if new_compression == 'low':
                new_compression = 90
            elif new_compression == 'high':
                new_compression = 70
            if new_format == 'png': new_compression = None
            with Image.open(file_location) as im:
                im = im.resize((new_resolution, new_resolution), Image.Resampling.BICUBIC)
                im.save(file_location, new_format, quality=new_compression)
    except KeyboardInterrupt:
        if os.path.isfile(file_location):
            print(f'\tDeleting partially downloaded file "{str(file_location)}"')
            silentremove(file_location)
        raise KeyboardInterrupt
    
    # Return the file location on successful download
    return file_location

# root mean square code by Charlie Clark: https://code.activestate.com/recipes/577630-comparing-two-images/
def compare_images(image_1, image_2):
    with Image.open(image_1) as im1, Image.open(image_2) as im2:
        h = ImageChops.difference(im1, im2).convert('L').histogram()
        return math.sqrt(reduce(operator.add, map(lambda h, i: h*(i**2), h, range(256))) / (float(im1.size[0]) * im1.size[1]))

# TODO: check if not closing the files causes issues, and see if there's a way to use the context manager with lambda expressions
get_image_resolution = lambda image_location : Image.open(image_location).size[0]

def silentremove(filename):
    try:
        os.remove(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

def read_temporary_setting(settings_location, module, root_setting=None, setting=None, global_mode=False):
    temporary_settings = pickle.load(open(settings_location, 'rb'))
    module_settings = temporary_settings['modules'][module] if module in temporary_settings['modules'] else None
    
    if module_settings:
        if global_mode:
            session = module_settings
        else:
            session = module_settings['sessions'][module_settings['selected']]
    else:
        session = None

    if session and root_setting:
        if setting:
            return session[root_setting][setting] if root_setting in session and setting in session[root_setting] else None
        else:
            return session[root_setting] if root_setting in session else None
    elif root_setting and not session:
        raise Exception('Module does not use temporary settings') 
    else:
        return session

def set_temporary_setting(settings_location, module, root_setting, setting=None, value=None, global_mode=False):
    temporary_settings = pickle.load(open(settings_location, 'rb'))
    module_settings = temporary_settings['modules'][module] if module in temporary_settings['modules'] else None

    if module_settings:
        if global_mode:
            session = module_settings
        else:
            session = module_settings['sessions'][module_settings['selected']]
    else:
        session = None

    if not session:
        raise Exception('Module does not use temporary settings')
    if setting:
        session[root_setting][setting] = value
    else:
        session[root_setting] = value
    pickle.dump(temporary_settings, open(settings_location, 'wb'))

create_temp_filename = lambda : f'temp/{os.urandom(16).hex()}'

def save_to_temp(input: bytes):
    location = create_temp_filename()
    open(location, 'wb').write(input)
    return location

def download_to_temp(url, headers={}, extension='', enable_progress_bar=False, indent_level=0):
    location = create_temp_filename() + (('.' + extension) if extension else '')
    download_file(url, location, headers=headers, enable_progress_bar=enable_progress_bar, indent_level=indent_level)
    return location

async def download_to_temp_async(session, url, headers={}, extension='', enable_progress_bar=False, indent_level=0):
    """Async version of download_to_temp"""
    location = create_temp_filename() + (('.' + extension) if extension else '')
    await download_file_async(session, url, location, headers=headers, enable_progress_bar=enable_progress_bar, indent_level=indent_level)
    return location


def open_url_in_browser(url):
    """Open a URL in the user's default web browser (used by modules for OAuth login flows)."""
    import webbrowser
    try:
        return webbrowser.open(url)
    except Exception:
        return False
