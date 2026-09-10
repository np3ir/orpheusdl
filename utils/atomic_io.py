"""Small atomic-file and inter-process locking primitives (no extra dependencies)."""
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import pickle
import tempfile
import time


@contextmanager
def file_lock(target, timeout=30.0):
    """Serialize operations on a canonical path; the OS releases locks on exit.

    Lock files are stable and intentionally retained in the user's temp directory.
    Never lock the data inode itself: atomic replacement changes that inode.
    """
    key = os.path.normcase(os.path.realpath(os.fspath(target)))
    directory = Path(tempfile.gettempdir()) / 'orpheus-file-locks'
    directory.mkdir(exist_ok=True)
    lock_path = directory / (hashlib.sha256(os.fsencode(key)).hexdigest() + '.lock')
    with open(lock_path, 'a+b') as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'Timed out waiting for file lock: {target}') from exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def temporary_sibling(target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.orpheus-', suffix='.part' + target.suffix,
                                dir=target.parent)
    os.close(fd)
    return name


def remove_temporary(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def atomic_pickle_write(target, value):
    """Caller holds file_lock over the complete read/modify/write transaction."""
    temporary = temporary_sibling(target)
    try:
        with open(temporary, 'wb') as handle:
            pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        remove_temporary(temporary)


def read_pickle(target):
    try:
        with open(target, 'rb') as handle:
            value = pickle.load(handle)
    except FileNotFoundError:
        return {'modules': {}}
    if not isinstance(value, dict) or not isinstance(value.get('modules'), dict):
        raise ValueError('Invalid session storage structure; original file was preserved')
    return value


@contextmanager
def session_transaction(target):
    with file_lock(target):
        value = read_pickle(target)
        yield value
        atomic_pickle_write(target, value)


def publish_download(temporary, target, skip_if_exists):
    """First completed transfer wins when skipping; otherwise last commit wins."""
    with file_lock(target):
        if skip_if_exists and os.path.isfile(target):
            return False
        os.replace(temporary, target)
        return True
