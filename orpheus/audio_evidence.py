"""Read-only audio evidence shared by download deduplication and cleanup.

Readable metadata is NOT a full audio decode/integrity check. Ambiguous evidence
must never authorize cleanup. Files changing during inspection are rejected.
"""
from dataclasses import asdict, dataclass
from pathlib import Path
import os

import mutagen


def canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def in_library(path, root):
    """Resolve links before containment checks; exclude quarantines and parts."""
    p, r = canonical(path), canonical(root)
    try:
        rel = Path(os.path.relpath(p, r))
        return (os.path.commonpath((p, r)) == r and p != r
                and '_duplicados' not in [part.lower() for part in rel.parts]
                and not Path(p).name.startswith('.orpheus-'))
    except (ValueError, OSError):
        return False


def tag_isrc(tags):
    for key in ('isrc', 'ISRC', 'TSRC', '----:com.apple.iTunes:ISRC'):
        if tags and key in tags:
            value = tags[key]
            if hasattr(value, 'text'):
                value = value.text
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ''
            if isinstance(value, bytes):
                value = value.decode('utf-8', 'strict')
            return str(value).strip().upper() or None
    return None


@dataclass(frozen=True)
class AudioEvidence:
    path: str
    isrc: str
    size: int
    mtime_ns: int
    duration: float
    channels: int
    lossless: bool
    bits: int
    sample_rate: int
    bitrate: int

    @property
    def rank(self):
        # Compressed lossless bitrate and file size do not measure fidelity.
        return (int(self.lossless), self.bits if self.lossless else 0,
                self.sample_rate, 0 if self.lossless else self.bitrate)

    def to_dict(self):
        return asdict(self)


def inspect_audio(path, root=None):
    """Return stable readable evidence, or None (missing/untagged/invalid)."""
    try:
        path = os.path.abspath(path)
        if root is not None and not in_library(path, root):
            return None
        before = os.stat(path)
        if before.st_size <= 0:
            return None
        audio = mutagen.File(path)
        info = getattr(audio, 'info', None)
        isrc = tag_isrc(getattr(audio, 'tags', None))
        if info is None or not isrc:
            return None
        duration = float(getattr(info, 'length', 0) or 0)
        rate = int(getattr(info, 'sample_rate', 0) or 0)
        channels = int(getattr(info, 'channels', 0) or 0)
        if duration <= 0 or rate <= 0 or channels <= 0:
            return None
        codec = str(getattr(info, 'codec', '')).lower()
        kind = type(audio).__name__.lower()
        lossless = kind in {'flac', 'wave', 'aiff', 'oggflac', 'wavpack', 'monkeysaudio'} or codec == 'alac'
        after = os.stat(path)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        return AudioEvidence(path, isrc, after.st_size, after.st_mtime_ns,
                             duration, channels, lossless,
                             int(getattr(info, 'bits_per_sample', 0) or 0), rate,
                             int(getattr(info, 'bitrate', 0) or 0))
    except (OSError, ValueError, TypeError, mutagen.MutagenError):
        return None


def compatible(a, b):
    """Same recording evidence, with a conservative two-second duration gate."""
    return a.isrc == b.isrc and a.channels == b.channels and abs(a.duration - b.duration) <= 2.0
