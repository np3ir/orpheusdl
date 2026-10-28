"""Stage downloads on a fast LOCAL disk and publish them to the real library afterwards.

Why: orpheus downloads and TAGS straight into the destination. On a network share (a NAS
of spinning disks) tag writes are many small random writes, and a run killed half way leaves
half-tagged files inside the library. With staging, download + tagging + conversion happen
in a local folder; the finished files are then copied to the library in one sequential
write each, verified (size + SHA-256 of what landed), published with an atomic rename, and
only then removed from the local folder.

Automatic: staging is used when the destination is a network location (UNC path or a
mapped network drive). Override with settings general.stage_network_downloads /
general.staging_dir, or the abq flags --no-stage / --stage-dir.

Safety rules (own implementation, same ideas as any careful file delivery):
  * never overwrite: identical content is reused (staged copy dropped); a different file
    with the same name gets a numbered name;
  * a failed or unverified copy leaves the staged file in place and reports it;
  * each run owns <staging_dir>/<run id>/ with a pid file, and only dead runs are recovered.
"""
import hashlib
import os
import shutil
import sys
import time

CHUNK = 1024 * 1024
RETRY_DELAYS = (3, 8, 20, 45)          # seconds; "too many open files" on a busy NAS clears by itself
_ORPHAN_PARTS = set()                  # our own temp files that could not be removed after a failure


def _is_handle_limit(exc):
    """EMFILE / Windows ERROR_TOO_MANY_OPEN_FILES (4): the NAS or the SMB client ran out of open handles."""
    return isinstance(exc, OSError) and (getattr(exc, 'errno', None) == 24 or getattr(exc, 'winerror', None) == 4)


def _remove_quietly(path, tries=3):
    for i in range(tries):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if i + 1 < tries:
                time.sleep(1)
    return False


def sweep_orphan_parts():
    """Try again to remove temp files left by failed copies (this process only)."""
    for path in list(_ORPHAN_PARTS):
        if _remove_quietly(path, tries=1):
            _ORPHAN_PARTS.discard(path)
TEMP_PREFIX = '.orpheus-'
PUB_PREFIX = '.orpheus-pub-'
AUDIO_EXT = {'.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav', '.aac', '.alac'}


# ---------------------------------------------------------------- detection
def is_network_path(path):
    """True for a UNC path or a drive letter Windows reports as a network drive."""
    p = os.path.abspath(str(path or ''))
    if p.startswith('\\\\') and not p.startswith('\\\\?\\'):
        return True
    if sys.platform != 'win32' or len(p) < 2 or p[1] != ':':
        return False
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(p[:2] + '\\') == 4   # DRIVE_REMOTE
    except Exception:
        return False


def resolve_stage_dir(settings, out_path, no_stage=False, stage_dir_arg=None, project_dir=None,
                      network_check=is_network_path):
    """Staging directory to use, or None when downloads should go straight to out_path."""
    if no_stage or not out_path:
        return None
    general = ((settings or {}).get('global') or {}).get('general') or {}
    default_dir = os.path.join(project_dir or os.getcwd(), 'temp', 'staging')
    if stage_dir_arg:                                   # explicit: force staging there
        return os.path.abspath(stage_dir_arg)
    if not general.get('stage_network_downloads', True):
        return None
    if not network_check(out_path):
        return None
    return os.path.abspath(general.get('staging_dir') or default_dir)


# ---------------------------------------------------------------- helpers
def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259                              # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def _numbered(path):
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f'{base} ({n}){ext}'):
        n += 1
    return f'{base} ({n}){ext}'


def _iter_files(root):
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            if name.startswith(TEMP_PREFIX) or name == 'run.pid':
                continue                                  # partial download / bookkeeping
            yield os.path.join(dirpath, name)


def copy_verified(src, dst):
    """Copy src to dst through a temp file next to dst, verify, then rename atomically.
    Returns the final path. Raises OSError when the copy cannot be verified (dst untouched)."""
    folder = os.path.dirname(dst)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f'{PUB_PREFIX}{os.getpid()}-{int(time.time() * 1000) % 10**8}.part')
    h = hashlib.sha256()
    try:
        with open(src, 'rb') as fi, open(tmp, 'wb') as fo:
            for block in iter(lambda: fi.read(CHUNK), b''):
                h.update(block)
                fo.write(block)
            fo.flush()
            try:
                os.fsync(fo.fileno())
            except OSError:
                pass
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise OSError('size mismatch after copy')
        if _sha256(tmp) != h.hexdigest():
            raise OSError('checksum mismatch after copy')
        os.replace(tmp, dst)
        return dst
    except BaseException:
        if not _remove_quietly(tmp):
            _ORPHAN_PARTS.add(tmp)          # removed later by sweep_orphan_parts()
        raise


def _evidence(path):
    try:
        from orpheus.audio_evidence import inspect_audio
        return inspect_audio(path)
    except Exception:
        return None


def _library_has(index, ev):
    """True when the library already holds this same recording (same ISRC, channels, duration +-2 s)."""
    try:
        from orpheus.audio_evidence import compatible
        return any(compatible(c, ev) for c in index.candidates(ev.isrc))
    except Exception:
        return False


def publish_dir(src_root, dst_root, index=None, log=print, copy=None, evidence_of=None):
    """Move every finished file under src_root into dst_root (same relative paths).

    Returns {'moved','reused','skipped','failed':[(path, why)]}. Never overwrites.
    With an ISRC index, each audio file is delivered under that ISRC's claim: when another run
    (or an earlier download) already put the same recording in the library, the staged copy is
    dropped instead of creating a numbered duplicate."""
    import contextlib
    copy = copy or copy_verified          # looked up per call, so it can be replaced
    evidence_of = evidence_of or _evidence
    stats = {'moved': 0, 'reused': 0, 'skipped': 0, 'failed': []}
    for src in list(_iter_files(src_root)):
        rel = os.path.relpath(src, src_root)
        dst = os.path.join(dst_root, rel)
        ev = evidence_of(src) if (index is not None and os.path.splitext(src)[1].lower() in AUDIO_EXT) else None
        outcome = None
        failure = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                with (index.claim(ev.isrc) if ev else contextlib.nullcontext()):
                    if ev and _library_has(index, ev):
                        os.remove(src)
                        outcome = 'skipped'
                        log(f'  [stage] {rel}: the library already has this recording; staged copy dropped')
                        break
                    target = dst
                    if os.path.exists(target):
                        if os.path.getsize(target) == os.path.getsize(src) and _sha256(target) == _sha256(src):
                            os.remove(src)                 # identical: reuse what is already there
                            outcome = 'reused'
                            break
                        target = _numbered(target)         # different content, same name
                    final = copy(src, target)
                    if ev:
                        try:
                            index.add(ev.isrc, final)
                        except Exception:
                            pass
                    outcome = 'moved'
                    break
            except Exception as e:
                failure = e
                if _is_handle_limit(e) and attempt < len(RETRY_DELAYS):
                    wait = RETRY_DELAYS[attempt]
                    log(f'  [stage] the destination reports too many open files; retrying {rel} in {wait}s')
                    time.sleep(wait)
                    continue
                break
        if outcome is None:
            stats['failed'].append((src, str(failure)))
            log(f'  [stage] could not deliver {rel}: {failure} (kept in staging)')
            continue
        if outcome in ('skipped', 'reused'):
            stats[outcome] += 1
            continue
        try:
            os.remove(src)
        except OSError:
            pass
        stats['moved'] += 1
    sweep_orphan_parts()
    _prune_empty(src_root)
    return stats


def delivery_lock(staging_dir, final_root):
    """Cross-process lock: one abq instance delivers to a given library at a time."""
    from utils.atomic_io import file_lock
    key = hashlib.sha256(os.path.normcase(os.path.abspath(final_root)).encode('utf-8')).hexdigest()[:16]
    return file_lock(os.path.join(staging_dir, f'.delivery-{key}'), timeout=3600)


def _prune_empty(root):
    for dirpath, _dirs, _names in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


# ---------------------------------------------------------------- run lifecycle
class Stage:
    """One abq run's staging area: <staging_dir>/<run id>/, owned by this process."""

    def __init__(self, staging_dir, final_root, config_dir, log=print, open_index=None):
        self.final_root = final_root
        self.config_dir = config_dir
        self.log = log
        # short name: the staged file's full path must stay well under the Windows path limit
        name = f'{os.getpid():x}'
        n = 1
        while os.path.exists(os.path.join(staging_dir, name)):      # leftover of a dead run with a reused pid
            n += 1
            name = f'{os.getpid():x}-{n}'
        self.run_dir = os.path.join(staging_dir, name)
        os.makedirs(self.run_dir, exist_ok=True)
        with open(os.path.join(self.run_dir, 'run.pid'), 'w') as f:
            f.write(str(os.getpid()))
        self.stats = {'moved': 0, 'reused': 0, 'skipped': 0, 'failed': []}
        self._open_index = open_index or self._default_index
        self.staging_dir = staging_dir

    def _default_index(self):
        from orpheus.isrc_library_index import IsrcLibraryIndex
        idx = IsrcLibraryIndex(self.final_root, self.config_dir, print_fn=lambda *a, **k: None)
        if idx.is_built():
            return idx
        idx.close()
        return None

    def _delivery_lock(self):
        return delivery_lock(self.staging_dir, self.final_root)

    def publish(self):
        """Deliver everything finished so far; safe to call after every orpheus batch."""
        idx = None
        try:
            with self._delivery_lock():
                idx = self._open_index()
                s = publish_dir(self.run_dir, self.final_root, idx, self.log)
        finally:
            if idx is not None:
                idx.close()
        self.stats['moved'] += s['moved']
        self.stats['reused'] += s['reused']
        self.stats['skipped'] += s.get('skipped', 0)
        self.stats['failed'] += s['failed']
        return s

    def finish(self):
        """Final delivery, then remove this run's staging folder and its throw-away index."""
        self.publish()
        self._drop_run_index(self.run_dir, self.config_dir)
        left = [p for p in _iter_files(self.run_dir)]
        if not left:
            shutil.rmtree(self.run_dir, ignore_errors=True)
        else:
            try:
                os.remove(os.path.join(self.run_dir, 'run.pid'))   # dead run: next start recovers it
            except OSError:
                pass
            self.log(f'  [stage] {len(left)} file(s) could not be delivered and stay in {self.run_dir}; '
                     f'they are retried automatically the next time abq runs.')
        return self.stats

    @staticmethod
    def _drop_run_index(run_dir, config_dir):
        """orpheus creates one small ISRC index per output root; remove this run's."""
        try:
            from orpheus.isrc_library_index import IsrcLibraryIndex
            idx = IsrcLibraryIndex(run_dir, config_dir)
            path = idx.db_path
            idx.close()
            for ext in ('', '-wal', '-shm'):
                try:
                    os.remove(path + ext)
                except OSError:
                    pass
        except Exception:
            pass


def recover_leftovers(staging_dir, final_root, config_dir, log=print, open_index=None):
    """Publish files that dead earlier runs left in staging. Live runs are never touched."""
    if not os.path.isdir(staging_dir):
        return 0
    recovered = 0
    for name in sorted(os.listdir(staging_dir)):
        run = os.path.join(staging_dir, name)
        if not os.path.isdir(run):
            continue
        pid_file = os.path.join(run, 'run.pid')
        try:
            with open(pid_file) as f:
                owner = f.read().strip()
        except OSError:
            owner = ''
        if owner and pid_alive(owner):
            continue
        files = list(_iter_files(run))
        if not files:
            shutil.rmtree(run, ignore_errors=True)
            Stage._drop_run_index(run, config_dir)
            continue
        log(f'  [stage] delivering {len(files)} file(s) left by an earlier run ({name})')
        idx = None
        try:
            if open_index:
                idx = open_index()
            else:
                from orpheus.isrc_library_index import IsrcLibraryIndex
                cand = IsrcLibraryIndex(final_root, config_dir, print_fn=lambda *a, **k: None)
                if cand.is_built():
                    idx = cand
                else:
                    cand.close()
            with delivery_lock(staging_dir, final_root):
                s = publish_dir(run, final_root, idx, log)
        finally:
            if idx is not None:
                idx.close()
        recovered += s['moved'] + s['reused'] + s.get('skipped', 0)
        if not list(_iter_files(run)):
            shutil.rmtree(run, ignore_errors=True)
            Stage._drop_run_index(run, config_dir)
    return recovered
