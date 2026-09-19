"""Small shared sync/async coordination layer; no downloads or library scans."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import logging
import os
import time

from utils.atomic_io import file_lock


def track_isrc(info):
    return str(getattr(getattr(info, 'tags', None), 'isrc', None) or '').strip().upper()


async def finish_io(awaitable):
    """Don't release a claim/close SQLite while a cancelled worker still writes."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Threads cannot be cancelled. Settle the operation before unwinding
        # the enclosing ISRC guard; cancellation is still propagated.
        try:
            await task
        except Exception:
            logging.exception('Worker failed while settling cancellation')
        raise


@contextmanager
def track_guard(downloader, info):
    idx = downloader._get_isrc_library_index() if track_isrc(info) else None
    try:
        if idx is None:
            yield None
        else:
            with idx.claim(track_isrc(info)):
                yield idx
    finally:
        if idx is not None:
            idx.close()


@asynccontextmanager
async def track_guard_async(downloader, info):
    # Acquisition is nonblocking: cancellation cannot strand a worker holding
    # a lock, and another async track can continue while this one waits.
    idx = downloader._get_isrc_library_index() if track_isrc(info) else None
    lock = None
    try:
        if idx is not None:
            deadline = time.monotonic() + 3600
            while True:
                attempt = file_lock(idx.lock_path(track_isrc(info)), timeout=0)
                try:
                    attempt.__enter__()
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Timed out waiting for ISRC download')
                    await asyncio.sleep(0.1)
                else:
                    lock = attempt
                    break
        yield idx
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)
        if idx is not None:
            idx.close()


def decision(idx, info, location, upgrade=False):
    """Return (existing match, safe proposed location).

    Quality upgrades are additive: an older copy is retained for planned cleanup.
    No overwrite/deletion is authorized by this helper. Unknown duration or
    spatial codecs are not compared across folders.
    """
    if idx is None:
        return None, location
    duration = float(getattr(info, 'duration', 0) or 0)
    codec = getattr(getattr(info, 'codec', None), 'name', '').upper()
    if duration <= 0 or codec not in {'FLAC', 'ALAC', 'WAV', 'MP3', 'AAC', 'OPUS', 'VORBIS'}:
        return None, location
    candidates = [e for e in idx.candidates(track_isrc(info))
                  if abs(e.duration - duration) <= 2 and e.channels == 2]
    if not candidates:
        return None, location
    best = candidates[0]
    lossless = codec in {'FLAC', 'ALAC', 'WAV'}
    bits = int(getattr(info, 'bit_depth', 0) or 0)
    rate = int(float(getattr(info, 'sample_rate', 0) or 0) * 1000)
    incoming = (int(lossless), bits if lossless else 0, rate,
                0 if lossless else int(getattr(info, 'bitrate', 0) or 0) * 1000)
    if upgrade and lossless and bits > 0 and rate > 0 and incoming > best.rank:
        if os.path.exists(location):
            base, ext = os.path.splitext(location)
            location = f'{base} ({bits}bit-{rate}Hz){ext}'
        return None, location
    return best.path, location


def register_completed(idx, info, location):
    if idx is None:
        return
    try:
        if not idx.add(track_isrc(info), location):
            logging.warning('Completed audio was not indexed: missing/mismatched ISRC or unreadable metadata: %s', location)
    except Exception:
        # Audio is already complete; do not misreport a successful transfer as
        # failed because the local index is busy/unavailable. Never hide it.
        logging.exception('Could not register completed audio in ISRC index: %s', location)
