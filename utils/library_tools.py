"""Helpers shared by the library clean-up scripts (dedup_numbered, merge_dupe_albums)."""
import os

from mutagen import File as MutFile

LOSSLESS_EXTS = {'.flac', '.wav'}


def isrc_of(path):
    """The track's ISRC (upper case) or None when it has none / cannot be read."""
    try:
        mf = MutFile(path)
        tags = getattr(mf, 'tags', None)
        if not tags:
            return None
        for k in ('isrc', 'ISRC', 'TSRC', '----:com.apple.iTunes:ISRC'):
            if k in tags:
                v = tags[k]
                v = v[0] if isinstance(v, (list, tuple)) and v else v
                if hasattr(v, 'text'):
                    t = v.text
                    v = t[0] if isinstance(t, (list, tuple)) and t else t
                if isinstance(v, bytes):
                    v = v.decode('utf-8', 'ignore')
                v = str(v).strip().upper()
                if v:
                    return v
    except Exception:
        return None
    return None


def quality_key(path):
    """Sortable quality of an audio file: lossless first, then bit depth, sample rate,
    bitrate, size. A bigger tuple is the better copy."""
    ext = os.path.splitext(path)[1].lower()
    bits = rate = bitrate = 0
    try:
        info = MutFile(path).info
        bits = getattr(info, 'bits_per_sample', 0) or 0
        rate = getattr(info, 'sample_rate', 0) or 0
        bitrate = getattr(info, 'bitrate', 0) or 0
    except Exception:
        pass
    return (ext in LOSSLESS_EXTS, bits, rate, bitrate, os.path.getsize(path))
