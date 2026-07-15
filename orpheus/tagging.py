import base64
import logging
import os
import shutil
import tempfile
import re
import unicodedata
from dataclasses import asdict

from PIL import Image
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType, APIC, USLT, TDAT, COMM, TPUB
from mutagen.mp3 import EasyMP3
from mutagen.mp4 import MP4Cover
from mutagen.mp4 import MP4Tags
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.oggvorbis import OggVorbisHeaderError

from utils.exceptions import *
from utils.models import ContainerEnum, TrackInfo

_FEAT_KEYWORDS = r'(?:feat|ft|fea|featuring|with|w/|con|junto a|prod(?:\.|uced by)?)'

_RE_FEAT_PARENS = re.compile(
    r'\s*[\(\[]\s*' + _FEAT_KEYWORDS + r'\.?\s+(.*?)[\)\]]',
    re.IGNORECASE
)
_RE_FEAT_DASH = re.compile(
    r'\s+[-–]\s+' + _FEAT_KEYWORDS + r'\.?\s+(.*)$',
    re.IGNORECASE
)

def _normalize_artist_name(name: str) -> str:
    """Clave de comparación de artistas: sin acentos, sin mayúsculas, espacios
    colapsados — "Rosalia" == "ROSALÍA". Paridad con dedup_artists de
    music_downloader.py (local aquí para evitar import circular)."""
    decomposed = unicodedata.normalize("NFKD", str(name))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def dedup_artists(names, exclude=()):
    """Quita artistas duplicados (por _normalize_artist_name), preservando el
    orden y la PRIMERA grafía vista."""
    seen = {_normalize_artist_name(n) for n in exclude}
    out = []
    for n in names:
        key = _normalize_artist_name(n)
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def _clean_title(title: str, artists: list) -> str:
    """Remove feat. from title if the featured artist is already in the artists list.
    Handles both parenthetical (feat. X) and dash separator (- feat. X) patterns.
    Also handles multi-artist feat strings like 'feat. Ozuna & Wisin'."""
    if not title:
        return title

    def _norm(s):
        d = unicodedata.normalize('NFD', s)
        s = ''.join(c for c in d if unicodedata.category(c) != 'Mn').lower()
        return re.sub(r'[\W_]+', '', s)

    artists_set = {_norm(a) for a in (artists or []) if a}

    def _known(name_norm: str) -> bool:
        if len(name_norm) <= 2:
            return False
        if name_norm in artists_set:
            return True
        # Some tracks have a single MAIN artist whose name is already a
        # compound "Artist feat. X, Y & Z" string (Tidal data quirk — no
        # separate FEATURED entries at all). In that case X/Y/Z are
        # substrings of that one artist entry, not separate set members.
        return any(name_norm in a for a in artists_set)

    def _feat_in_artists(inner: str) -> bool:
        # First: try the whole string (handles duos like "Zion & Lennox" whose name contains &)
        inner_norm = _norm(inner.strip())
        if inner_norm and _known(inner_norm):
            return True
        # Second: split by separators and check each name individually (e.g. "Ozuna & Wisin")
        feat_names = re.split(r'\s*[,&]\s*|\s+(?:and|y)\s+', inner.strip(), flags=re.IGNORECASE)
        if len(feat_names) > 1:
            return all(_known(_norm(n)) for n in feat_names)
        return False

    match = _RE_FEAT_PARENS.search(title)
    if match and _feat_in_artists(match.group(1)):
        title = title.replace(match.group(0), '').strip()

    match = _RE_FEAT_DASH.search(title)
    if match and _feat_in_artists(match.group(1)):
        title = title[:match.start()].strip()

    return title

# Needed for Windows tagging support
MP4Tags._padding = 0


def _save_flac_smb_safe(tagger, file_path: str) -> None:
    """Save FLAC tags via temp file — fallback for Windows SMB shares where seek()+write() is unreliable."""
    from pathlib import Path
    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.flac')
    tmp = Path(tmp_path)
    try:
        os.close(tmp_fd)
        shutil.copy2(file_path, str(tmp))
        tmp_tagger = FLAC(str(tmp))
        tmp_tagger.clear()
        tmp_tagger.update(dict(tagger))
        tmp_tagger.clear_pictures()
        for pic in tagger.pictures:
            tmp_tagger.add_picture(pic)
        tmp_tagger.save()
        shutil.move(str(tmp), file_path)
        logging.debug(f"FLAC metadata saved via temp file for {Path(file_path).name}")
    except Exception as e2:
        logging.warning(f"FLAC metadata save via temp file failed for {Path(file_path).name}: {e2}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _resize_image_if_needed(image_path: str, max_size_bytes: int = 16 * 1024 * 1024,
                            target_resolution: tuple = (3000, 3000)) -> str:
    """Resize an image if it exceeds the maximum file size."""
    if not image_path or not os.path.exists(image_path):
        return image_path
    if os.path.getsize(image_path) <= max_size_bytes:
        return image_path

    try:
        with Image.open(image_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail(target_resolution, Image.Resampling.LANCZOS)
            temp_fd, temp_path = tempfile.mkstemp(suffix='.jpg', prefix='orpheus_resized_')
            os.close(temp_fd)
            img.save(temp_path, 'JPEG', quality=90, optimize=True)
            return temp_path
    except Exception as e:
        print(f'\tFailed to resize cover image: {e}. Using original image.')
        return image_path


def tag_file(file_path: str, image_path: str, track_info: TrackInfo, credits_list: list, embedded_lyrics: str,
             container: ContainerEnum):
    """Tag file with metadata - Clean version without logging"""

    # Basic validation
    if track_info is None:
        return

    # mutagen doesn't support the Windows extended-path prefix (\\?\)
    if file_path.startswith('\\\\?\\'):
        file_path = file_path[4:]
    
    # Get original values
    titulo_original = track_info.name if hasattr(track_info, 'name') else ""
    artistas_original = track_info.artists if hasattr(track_info, 'artists') else []
    album_original = ""
    
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'album'):
        album_original = track_info.tags.album
    
    if not album_original and hasattr(track_info, 'album'):
        album_original = track_info.album
    
    if not album_original:
        album_original = "Unknown Album"

    bpm_val = None
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'bpm'):
        bpm_val = track_info.tags.bpm

    # Open file with mutagen
    if container == ContainerEnum.flac:
        tagger = FLAC(file_path)
    elif container == ContainerEnum.opus:
        tagger = OggOpus(file_path)
    elif container == ContainerEnum.ogg:
        tagger = OggVorbis(file_path)
    elif container == ContainerEnum.mp3:
        tagger = EasyMP3(file_path)
        if tagger.tags is None:
            tagger.tags = EasyID3()
        tagger.tags.RegisterTextKey('encoded', 'TSSE')
        tagger.tags.RegisterTXXXKey('compatible_brands', 'compatible_brands')
        tagger.tags.RegisterTXXXKey('major_brand', 'major_brand')
        tagger.tags.RegisterTXXXKey('minor_version', 'minor_version')
        tagger.tags.RegisterTXXXKey('Rating', 'Rating')
        tagger.tags.RegisterTXXXKey('upc', 'BARCODE')
        tagger.tags.pop('encoded', None)
    elif container == ContainerEnum.m4a:
        tagger = EasyMP4(file_path)
        tagger.RegisterTextKey('isrc', '----:com.apple.itunes:ISRC')
        tagger.RegisterTextKey('upc', '----:com.apple.itunes:UPC')
        tagger.RegisterTextKey('explicit', 'rtng') if track_info.explicit is not None else None
        tagger.RegisterTextKey('covr', 'covr')
        tagger.RegisterTextKey('lyrics', '\xa9lyr') if embedded_lyrics else None
    else:
        raise Exception('Unknown container for tagging')
    
    # Remove useless MPEG-DASH ffmpeg tags
    if tagger.tags is not None:
        if 'major_brand' in tagger.tags:
            del tagger.tags['major_brand']
        if 'minor_version' in tagger.tags:
            del tagger.tags['minor_version']
        if 'compatible_brands' in tagger.tags:
            del tagger.tags['compatible_brands']
        if 'encoder' in tagger.tags:
            del tagger.tags['encoder']
    
    # Title — clean version (same as filename)
    titulo_clean = _clean_title(titulo_original, artistas_original if isinstance(artistas_original, list) else [artistas_original])
    if titulo_clean:
        tagger['title'] = str(titulo_clean)

    # Artist — joined with " / " (same separator as filename).
    # Dedup normalizado (acentos/mayúsculas) para no repetir el mismo artista
    # con distinta grafía (paridad tiddl/streamrip).
    if artistas_original:
        if isinstance(artistas_original, list):
            artist_list = dedup_artists(str(a) for a in artistas_original if a)
        else:
            artist_list = [str(artistas_original)]
        tagger['artist'] = artist_list
    
    if album_original:
        tagger['album'] = str(album_original)
    
    # Album artist
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'album_artist') and track_info.tags.album_artist:
        tagger['albumartist'] = str(track_info.tags.album_artist)
    
    # Track / disc number — solo número, sin totales (parity tiddl: trkn/disk=(n,0), sin TRACKTOTAL/DISCTOTAL)
    if hasattr(track_info, 'tags') and track_info.tags:
        if hasattr(track_info.tags, 'track_number') and track_info.tags.track_number:
            tagger['tracknumber'] = str(track_info.tags.track_number)
        if hasattr(track_info.tags, 'disc_number') and track_info.tags.disc_number:
            tagger['discnumber'] = str(track_info.tags.disc_number)
    
    # Date
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'release_date') and track_info.tags.release_date:
        if container == ContainerEnum.mp3:
            release_dd_mm = f'{track_info.tags.release_date[8:10]}{track_info.tags.release_date[5:7]}'
            tagger.tags._EasyID3__id3._DictProxy__dict['TDAT'] = TDAT(encoding=3, text=release_dd_mm)
            tagger['date'] = str(track_info.release_year) if hasattr(track_info, 'release_year') and track_info.release_year else track_info.tags.release_date[:4]
        else:
            tagger['date'] = str(track_info.tags.release_date)[:4]
    elif hasattr(track_info, 'release_year') and track_info.release_year:
        tagger['date'] = str(track_info.release_year)
    
    # Copyright
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'copyright') and track_info.tags.copyright:
        tagger['copyright'] = str(track_info.tags.copyright)
    
    # Genre
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'genres') and track_info.tags.genres:
        tagger['genre'] = track_info.tags.genres

    # BPM (FLAC/OGG/MP3 inline; M4A tmpo is written after save via raw MP4 pass)
    if bpm_val:
        try:
            if container in {ContainerEnum.flac, ContainerEnum.ogg, ContainerEnum.opus}:
                tagger['BPM'] = str(int(float(bpm_val)))
            elif container == ContainerEnum.mp3:
                tagger['bpm'] = str(int(float(bpm_val)))
        except (ValueError, TypeError):
            pass

    # ISRC
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'isrc') and track_info.tags.isrc:
        tagger['isrc'] = track_info.tags.isrc.encode() if container == ContainerEnum.m4a else track_info.tags.isrc
    
    # Description
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'description') and track_info.tags.description and container == ContainerEnum.m4a:
        tagger.RegisterTextKey('desc', 'description')
        tagger['description'] = track_info.tags.description
    
    # Comment
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'comment') and track_info.tags.comment:
        if container == ContainerEnum.m4a:
            tagger.RegisterTextKey('comment', '\xa9cmt')
            tagger['comment'] = track_info.tags.comment
        elif container == ContainerEnum.mp3:
            tagger.tags._EasyID3__id3._DictProxy__dict['COMM'] = COMM(
                encoding=3, lang=u'eng', desc=u'', text=track_info.tags.comment)
    
    # Extra tags
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'extra_tags') and track_info.tags.extra_tags:
        if container in {ContainerEnum.flac, ContainerEnum.ogg}:
            for key, value in track_info.tags.extra_tags.items():
                tagger[key] = value
        elif container is ContainerEnum.m4a:
            for key, value in track_info.tags.extra_tags.items():
                tagger.RegisterTextKey(key, '----:com.apple.itunes:' + key)
                tagger[key] = str(value).encode()
    
    # Credits
    if credits_list:
        if container == ContainerEnum.m4a:
            for credit in credits_list:
                tagger.RegisterTextKey(credit.type, '----:com.apple.itunes:' + credit.type)
                tagger[credit.type] = [con.encode() for con in credit.names]
        elif container == ContainerEnum.mp3:
            for credit in credits_list:
                tagger.tags.RegisterTXXXKey(credit.type.upper(), credit.type)
                tagger[credit.type] = credit.names
        else:
            for credit in credits_list:
                try:
                    raw_key = credit.type.upper()
                    normalized = unicodedata.normalize('NFKD', raw_key)
                    safe_key = normalized.encode('ascii', 'ignore').decode('ascii')
                    safe_key = re.sub(r'[=\x00-\x1f]', '', safe_key).strip()
                    if safe_key:
                        tagger[safe_key] = credit.names
                except Exception:
                    pass
    
    # Lyrics
    if embedded_lyrics:
        if container == ContainerEnum.mp3:
            tagger.tags._EasyID3__id3._DictProxy__dict['USLT'] = USLT(
                encoding=3, lang=u'eng', text=embedded_lyrics)
        else:
            tagger['lyrics'] = embedded_lyrics
    
    # Handle cover art
    if image_path and os.path.exists(image_path):
        # Clear existing cover art
        if container == ContainerEnum.flac:
            tagger.clear_pictures()
        elif container == ContainerEnum.m4a:
            if 'covr' in tagger:
                del tagger['covr']
        elif container == ContainerEnum.mp3:
            if hasattr(tagger.tags, '_EasyID3__id3') and 'APIC' in tagger.tags._EasyID3__id3:
                del tagger.tags._EasyID3__id3['APIC']
        elif container in {ContainerEnum.ogg, ContainerEnum.opus}:
            if 'metadata_block_picture' in tagger:
                del tagger['metadata_block_picture']
        
        # Embed new cover art
        resized_image_path = _resize_image_if_needed(image_path, max_size_bytes=16 * 1024 * 1024)
        temp_file_created = resized_image_path != image_path
        
        try:
            with open(resized_image_path, 'rb') as c:
                data = c.read()
            picture = Picture()
            picture.data = data
            
            if len(picture.data) < picture._MAX_SIZE:
                if container == ContainerEnum.flac:
                    picture.type = PictureType.COVER_FRONT
                    picture.mime = u'image/jpeg'
                    tagger.add_picture(picture)
                elif container == ContainerEnum.m4a:
                    tagger['covr'] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]
                elif container == ContainerEnum.mp3:
                    tagger.tags._EasyID3__id3._DictProxy__dict['APIC'] = APIC(
                        encoding=3, mime='image/jpeg', type=3, desc='Cover', data=data)
                elif container in {ContainerEnum.ogg, ContainerEnum.opus}:
                    im = Image.open(resized_image_path)
                    width, height = im.size
                    picture.type = 17
                    picture.desc = u'Cover Art'
                    picture.mime = u'image/jpeg'
                    picture.width = width
                    picture.height = height
                    picture.depth = 24
                    encoded_data = base64.b64encode(picture.write())
                    tagger['metadata_block_picture'] = [encoded_data.decode('ascii')]
            else:
                print(f'\tCover file size is still too large after resizing, only {(picture._MAX_SIZE / 1024 ** 2):.2f}MB are allowed. Track will not have cover saved.')
        finally:
            if temp_file_created and os.path.exists(resized_image_path):
                try:
                    os.unlink(resized_image_path)
                except OSError:
                    pass
    else:
        # Remove existing cover art when embed_cover is disabled
        if container == ContainerEnum.flac:
            tagger.clear_pictures()
        elif container == ContainerEnum.m4a:
            if 'covr' in tagger:
                del tagger['covr']
        elif container == ContainerEnum.mp3:
            if hasattr(tagger.tags, '_EasyID3__id3') and 'APIC' in tagger.tags._EasyID3__id3:
                del tagger.tags._EasyID3__id3['APIC']
        elif container in {ContainerEnum.ogg, ContainerEnum.opus}:
            if 'metadata_block_picture' in tagger:
                del tagger['metadata_block_picture']
    
    # Save file
    try:
        if container == ContainerEnum.flac:
            try:
                tagger.save()
            except Exception as _smb_err:
                logging.debug(f"Direct FLAC save failed ({_smb_err}), retrying via temp file...")
                _save_flac_smb_safe(tagger, file_path)
        elif container == ContainerEnum.mp3:
            tagger.save(file_path, v1=2, v2_version=3, v23_sep=None)
        else:
            tagger.save()
    except OggVorbisHeaderError as ogg_header_error:
        if "unable to read full header" in str(ogg_header_error).lower():
            logging.warning(f"Ignoring mutagen OggVorbisHeaderError ('unable to read full header') for {file_path}. File might be okay.")
        else:
            logging.error(f"Tagging failed for {file_path} with OggVorbisHeaderError: {ogg_header_error}", exc_info=True)
            tag_text = '\n'.join((f'{k}: {v}' for k, v in asdict(track_info.tags).items() if v and k != 'credits' and k != 'lyrics'))
            tag_text += '\n\ncredits:\n    ' + '\n    '.join(f'{credit.type}: {", ".join(credit.names)}' for credit in credits_list if credit.names) if credits_list else ''
            tag_text += '\n\nlyrics:\n    ' + '\n    '.join(embedded_lyrics.split('\n')) if embedded_lyrics else ''
            open(file_path.rsplit('.', 1)[0] + '_tags.txt', 'w', encoding='utf-8').write(tag_text)
            raise TagSavingFailure
    except Exception as e:
        logging.error(f"Generic tagging failed for {file_path}. Error: {e}", exc_info=True)
        tag_text = '\n'.join((f'{k}: {v}' for k, v in asdict(track_info.tags).items() if v and k != 'credits' and k != 'lyrics'))
        tag_text += '\n\ncredits:\n    ' + '\n    '.join(f'{credit.type}: {", ".join(credit.names)}' for credit in credits_list if credit.names) if credits_list else ''
        tag_text += '\n\nlyrics:\n    ' + '\n    '.join(embedded_lyrics.split('\n')) if embedded_lyrics else ''
        open(file_path.rsplit('.', 1)[0] + '_tags.txt', 'w', encoding='utf-8').write(tag_text)
        raise TagSavingFailure

    # BPM for M4A: tmpo is an integer atom not writable via EasyMP4 — second raw MP4 pass
    if bpm_val and container == ContainerEnum.m4a:
        try:
            from mutagen.mp4 import MP4 as _RawMP4
            _raw = _RawMP4(file_path)
            if _raw.tags is None:
                _raw.add_tags()
            _raw.tags['tmpo'] = [int(float(bpm_val))]
            _raw.save()
        except Exception as _bpm_e:
            logging.debug(f"Could not write BPM to M4A: {_bpm_e}")