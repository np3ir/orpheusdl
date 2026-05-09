import base64
import logging
import os
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

_RE_FEAT = re.compile(
    r'\s*\(\s*(?:feat|ft|featuring|with|con)\.?\s+.*?\)',
    re.IGNORECASE
)

def _clean_title(title: str, artists: list) -> str:
    """Remove feat. from title if the featured artist is already in the artists list."""
    if not title:
        return title
    # Remove feat. parenthetical if featured artist appears in artist list
    def _norm(s):
        d = unicodedata.normalize('NFD', s)
        return ''.join(c for c in d if unicodedata.category(c) != 'Mn').lower().strip()
    artists_norm = ' '.join(_norm(a) for a in (artists or []))
    match = _RE_FEAT.search(title)
    if match:
        feat_name = _norm(match.group(0))
        # Extract just the artist name from the feat. string
        inner = re.sub(r'^\s*\(\s*(?:feat|ft|featuring|with|con)\.?\s*', '', match.group(0), flags=re.IGNORECASE).rstrip(')')
        if len(inner.strip()) > 2 and _norm(inner) in artists_norm:
            title = title.replace(match.group(0), '').strip()
    return title

# Needed for Windows tagging support
MP4Tags._padding = 0


def _resize_image_if_needed(image_path: str, max_size_bytes: int = 16 * 1024 * 1024,
                            target_resolution: tuple = (3000, 3000)) -> str:
    """Resize an image if it exceeds the maximum file size."""
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

    # Artist — joined with " / " (same separator as filename)
    if artistas_original:
        if isinstance(artistas_original, list):
            artist_str = ' / '.join(str(a) for a in artistas_original if a)
        else:
            artist_str = str(artistas_original)
        tagger['artist'] = artist_str
    
    if album_original:
        tagger['album'] = str(album_original)
    
    # Album artist
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'album_artist') and track_info.tags.album_artist:
        tagger['albumartist'] = str(track_info.tags.album_artist)
    
    # Track number
    if hasattr(track_info, 'tags') and track_info.tags:
        if container == ContainerEnum.m4a or container == ContainerEnum.mp3:
            if hasattr(track_info.tags, 'track_number') and track_info.tags.track_number:
                if hasattr(track_info.tags, 'total_tracks') and track_info.tags.total_tracks:
                    tagger['tracknumber'] = str(track_info.tags.track_number) + '/' + str(track_info.tags.total_tracks)
                else:
                    tagger['tracknumber'] = str(track_info.tags.track_number)
            
            if hasattr(track_info.tags, 'disc_number') and track_info.tags.disc_number:
                if hasattr(track_info.tags, 'total_discs') and track_info.tags.total_discs:
                    tagger['discnumber'] = str(track_info.tags.disc_number) + '/' + str(track_info.tags.total_discs)
                else:
                    tagger['discnumber'] = str(track_info.tags.disc_number)
        else:
            if hasattr(track_info.tags, 'track_number') and track_info.tags.track_number:
                tagger['tracknumber'] = str(track_info.tags.track_number)
            if hasattr(track_info.tags, 'total_tracks') and track_info.tags.total_tracks:
                tagger['totaltracks'] = str(track_info.tags.total_tracks)
            if hasattr(track_info.tags, 'disc_number') and track_info.tags.disc_number:
                tagger['discnumber'] = str(track_info.tags.disc_number)
            if hasattr(track_info.tags, 'total_discs') and track_info.tags.total_discs:
                tagger['totaldiscs'] = str(track_info.tags.total_discs)
    
    # Date
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'release_date') and track_info.tags.release_date:
        if container == ContainerEnum.mp3:
            release_dd_mm = f'{track_info.tags.release_date[8:10]}{track_info.tags.release_date[5:7]}'
            tagger.tags._EasyID3__id3._DictProxy__dict['TDAT'] = TDAT(encoding=3, text=release_dd_mm)
            tagger['date'] = str(track_info.release_year) if hasattr(track_info, 'release_year') and track_info.release_year else track_info.tags.release_date[:4]
        else:
            tagger['date'] = str(track_info.tags.release_date)
    elif hasattr(track_info, 'release_year') and track_info.release_year:
        tagger['date'] = str(track_info.release_year)
    
    # Copyright
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'copyright') and track_info.tags.copyright:
        tagger['copyright'] = str(track_info.tags.copyright)
    
    # Explicit
    if hasattr(track_info, 'explicit') and track_info.explicit is not None:
        if container == ContainerEnum.m4a:
            tagger['explicit'] = b'\x01' if track_info.explicit else b'\x02'
        elif container == ContainerEnum.mp3:
            tagger['Rating'] = 'Explicit' if track_info.explicit else 'Clean'
        else:
            tagger['Rating'] = 'Explicit' if track_info.explicit else 'Clean'
    
    # Genre
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'genres') and track_info.tags.genres:
        tagger['genre'] = track_info.tags.genres
    
    # ISRC
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'isrc') and track_info.tags.isrc:
        tagger['isrc'] = track_info.tags.isrc.encode() if container == ContainerEnum.m4a else track_info.tags.isrc
    
    # UPC
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'upc') and track_info.tags.upc:
        tagger['UPC'] = track_info.tags.upc.encode() if container == ContainerEnum.m4a else track_info.tags.upc
    
    # Label
    if hasattr(track_info, 'tags') and track_info.tags and hasattr(track_info.tags, 'label') and track_info.tags.label:
        if container in {ContainerEnum.flac, ContainerEnum.ogg}:
            tagger['Label'] = track_info.tags.label
        elif container == ContainerEnum.mp3:
            tagger.tags._EasyID3__id3._DictProxy__dict['TPUB'] = TPUB(encoding=3, text=track_info.tags.label)
        elif container == ContainerEnum.m4a:
            tagger.RegisterTextKey('label', '\xa9pub')
            tagger['label'] = track_info.tags.label
    
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
                    tagger.tags[credit.type] = credit.names
                except:
                    pass
    
    # Lyrics
    if embedded_lyrics:
        if container == ContainerEnum.mp3:
            tagger.tags._EasyID3__id3._DictProxy__dict['USLT'] = USLT(
                encoding=3, lang=u'eng', text=embedded_lyrics)
        else:
            tagger['lyrics'] = embedded_lyrics
    
    # Replay gain
    if hasattr(track_info, 'tags') and track_info.tags:
        if hasattr(track_info.tags, 'replay_gain') and hasattr(track_info.tags, 'replay_peak'):
            if track_info.tags.replay_gain and track_info.tags.replay_peak and container != ContainerEnum.m4a:
                tagger['REPLAYGAIN_TRACK_GAIN'] = str(track_info.tags.replay_gain)
                tagger['REPLAYGAIN_TRACK_PEAK'] = str(track_info.tags.replay_peak)
    
    # Handle cover art
    if image_path:
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
        if container == ContainerEnum.mp3:
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