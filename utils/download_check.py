"""Sanity check of a freshly downloaded audio file (report only, never deletes).

Catches the silent failures a successful HTTP download can hide: an empty or tiny
file, an unreadable container (aborted/truncated stream), a ~30 s preview, or a
different edit/version of the song. The expected duration comes from the metadata
the service gave for the track being downloaded.

Rules (our own thresholds, tuned loose on purpose: metadata durations differ by a
few seconds between services and editions, so only a clear gap is flagged):
  * file missing or 0 bytes
  * lossless file under ~10 KB per expected second (header-only / HTML / aborted)
  * container cannot be read, or reports no length
  * duration gap larger than max(8 s, 12 % of the expected length)
Tracks expected shorter than 60 s skip the duration comparison (their metadata is
unreliable) but still get the existence / size / readable checks.
"""
import os

MIN_LOSSLESS_BYTES_PER_SECOND = 10_000
MIN_LOSSLESS_BYTES_FLOOR = 64 * 1024
SHORT_TRACK_SECONDS = 60
ABS_TOLERANCE_S = 8.0
REL_TOLERANCE = 0.12
LOSSLESS_EXT = ('.flac', '.m4a', '.wav', '.alac')


def _length_seconds(path):
    try:
        import mutagen
        mf = mutagen.File(path)
        length = getattr(getattr(mf, 'info', None), 'length', None)
        return float(length) if length else None
    except Exception:
        return None


def download_problem(path, expected_seconds=None):
    """Return a short reason string if the file looks wrong, else None."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 'file missing'
    if size == 0:
        return 'file is empty (0 bytes)'
    try:
        expected = float(expected_seconds) if expected_seconds else None
    except (TypeError, ValueError):
        expected = None
    if expected and path.lower().endswith(LOSSLESS_EXT):
        floor = max(MIN_LOSSLESS_BYTES_FLOOR, int(expected * MIN_LOSSLESS_BYTES_PER_SECOND))
        if size < floor:
            return f'only {size // 1024} KB for a {expected:.0f}s lossless track (truncated / not audio?)'
    actual = _length_seconds(path)
    if actual is None:
        return 'unreadable audio container (aborted or truncated download?)'
    if expected and expected >= SHORT_TRACK_SECONDS:
        tol = max(ABS_TOLERANCE_S, expected * REL_TOLERANCE)
        if abs(actual - expected) > tol:
            kind = 'shorter (preview / truncated?)' if actual < expected else 'longer (other version?)'
            return f'duration {actual:.0f}s vs expected {expected:.0f}s: {kind}'
    return None
