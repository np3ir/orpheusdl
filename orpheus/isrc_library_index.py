"""Library-wide ISRC index for cross-folder duplicate detection.

OrpheusDL's built-in dedup only compares a new track against the file that would
land at the SAME target path. This index lets the downloader (and a standalone
report tool) know every ISRC already present ANYWHERE under a library root, so the
same recording is not downloaded twice into different album/edition folders.

Design goals (per owner, 2026-09-19):
- The DISK is the source of truth: every positive match is re-verified against the
  real file before it is trusted. Stale entries never authorize skips.
- Cheap to consult repeatedly: the ISRC map is cached in a LOCAL SQLite db
  (never on the NAS) and refreshed incrementally by mtime/size, so re-runs and a
  batch of many `orpheus` processes don't re-read every tag.
- Scoped PER ROOT: the NAS root and the external-subset root each get their own db,
  so a track on the NAS never blocks a wanted copy on the external disk.
"""

import os
import sqlite3
import hashlib
import time
import threading
from contextlib import contextmanager
from utils.atomic_io import file_lock
from orpheus.audio_evidence import inspect_audio
from concurrent.futures import ThreadPoolExecutor

AUDIO_EXT = {'.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav', '.aac', '.alac'}


def read_isrc(file_path, root=None):
    evidence = inspect_audio(file_path, root)
    return evidence.isrc if evidence else None


class IsrcLibraryIndex:
    def __init__(self, root, db_dir, print_fn=None, refresh_ttl=900):
        self.root = os.path.abspath(root)
        self._norm_root = os.path.normcase(self.root).rstrip('\\/')
        os.makedirs(db_dir, exist_ok=True)
        key = hashlib.md5(self._norm_root.encode('utf-8')).hexdigest()[:12]
        self.db_path = os.path.join(db_dir, f'isrc_index_{key}.db')
        self.refresh_ttl = refresh_ttl
        self._print = print_fn or (lambda *_a, **_k: None)
        self._mutex = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._conn.execute('PRAGMA busy_timeout=30000')
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS files('
            'path TEXT PRIMARY KEY, mtime REAL, size INTEGER, isrc TEXT)')
        self._conn.execute(
            'CREATE INDEX IF NOT EXISTS ix_isrc ON files(isrc)')
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)')
        self._conn.commit()

    # ---- persistence helpers ----
    def _meta_get(self, k, default=None):
        row = self._conn.execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
        return row[0] if row else default

    def _meta_set(self, k, v):
        self._conn.execute('INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)', (k, str(v)))

    def row_count(self):
        return self._conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]

    def isrc_count(self):
        return self._conn.execute(
            'SELECT COUNT(DISTINCT isrc) FROM files WHERE isrc IS NOT NULL AND isrc != ""').fetchone()[0]

    def last_scan(self):
        v = self._meta_get('last_scan')
        return float(v) if v else 0.0

    def is_built(self):
        return self.last_scan() > 0

    # ---- the scan ----
    def _iter_audio(self):
        for dirpath, dirs, files in os.walk(self.root, onerror=self._scan_error):
            # Never index the dedupe quarantine — moved-aside copies must not
            # re-count as library duplicates.
            dirs[:] = [d for d in dirs if d.lower() != '_duplicados' and not os.path.islink(os.path.join(dirpath, d))]
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext in AUDIO_EXT and not name.startswith('.orpheus-'):
                    yield os.path.join(dirpath, name)

    def _scan_error(self, error):
        self._scan_errors.append(str(error))

    def build(self, force=False, workers=8):
        # One scanner per root; readers/writers use short SQLite transactions.
        with file_lock(self.db_path + '.scan', timeout=0):
            return self._build(force=force, workers=max(1, min(64, workers)))

    def _build(self, force=False, workers=8):
        """Full/incremental reconcile of the index against the disk.

        Only reads the ISRC tag of files that are new or whose mtime/size changed;
        drops rows whose file no longer exists. Tag reads run concurrently
        (`workers` threads) — over a NAS/SMB share, latency dominates, so many
        parallel reads cut a big first build from hours to a fraction. SQLite
        writes stay on the main thread. Returns a stats dict.
        """
        if not os.path.isdir(self.root):
            self._print(f'  [isrc-index] root no existe: {self.root}')
            return {'error': 'root_missing'}
        self._scan_errors = []
        known = {}
        for path, mtime, size, isrc in self._conn.execute('SELECT path, mtime, size, isrc FROM files'):
            known[path] = (mtime, size, isrc)
        seen = set()
        to_read = []  # (path, mtime, size) that are new or changed
        t0 = time.time()
        walked = 0
        for path in self._iter_audio():
            seen.add(path)
            try:
                st = os.stat(path)
            except OSError as exc:
                self._scan_error(exc)
                continue
            mtime, size = st.st_mtime, st.st_size
            prev = known.get(path)
            if prev and prev[2] and prev[0] == mtime and prev[1] == size and not force:
                continue  # unchanged
            to_read.append((path, mtime, size))
            walked += 1
            if walked % 5000 == 0:
                self._print(f'  [isrc-index] {walked} archivos nuevos/cambiados detectados...')
        added = sum(1 for p, _m, _s in to_read if p not in known)
        changed = len(to_read) - added

        done = 0
        # Bounded batches avoid allocating hundreds of thousands of futures.
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for offset in range(0, len(to_read), 256):
                batch = to_read[offset:offset + 256]
                values = list(ex.map(lambda path: read_isrc(path, self.root), [p for p, _, _ in batch]))
                with self._mutex, self._conn:
                    for (path, mtime, size), isrc in zip(batch, values):
                        # Recheck after reading; don't commit a moving target.
                        try:
                            st = os.stat(path)
                        except OSError as exc:
                            self._scan_error(exc)
                            continue
                        if (st.st_mtime, st.st_size) != (mtime, size):
                            self._scan_error('File changed during scan: ' + path)
                            continue
                        if not isrc and known.get(path, (None, None, None))[2]:
                            self._scan_error('Previously indexed audio is now unreadable/untagged: ' + path)
                            continue  # Preserve evidence for retry/review; find() revalidates it.
                        self._conn.execute(
                            'INSERT OR REPLACE INTO files(path,mtime,size,isrc) VALUES(?,?,?,?)',
                            (path, mtime, size, isrc))
                done += len(batch)
                self._print(f'  [isrc-index] {done}/{len(to_read)} tags inspected')
        # Incomplete NAS walks must not erase previously known entries or mark
        # an incomplete first build as usable. Re-run explicitly after recovery.
        removed = 0
        if not self._scan_errors:
            with self._mutex, self._conn:
                for path in known:
                    if path not in seen and not os.path.isfile(path):
                        self._conn.execute('DELETE FROM files WHERE path=?', (path,))
                        removed += 1
                self._meta_set('last_scan', time.time())
                self._meta_set('root', self.root)
        return {'total_files': len(seen), 'added': added, 'changed': changed,
                'removed': removed, 'isrcs': self.isrc_count(),
                'seconds': round(time.time() - t0, 1),
                **({'error': 'incomplete_scan', 'errors': self._scan_errors[:10]} if self._scan_errors else {})}

    def ensure_fresh(self):
        """Compatibility shim: downloads/reports never trigger a full NAS walk.

        Use isrc_index_tool.py --build explicitly to discover external changes.
        Positive matches are always inspected on disk, independently of age.
        """
        return self.is_built()

    def lock_path(self, isrc):
        key = hashlib.sha256(str(isrc).strip().upper().encode('utf-8')).hexdigest()
        return self.db_path + '.isrc-' + key

    @contextmanager
    def claim(self, isrc, timeout=3600):
        """Hold through lookup, transfer, tagging and completed-file registration.

        OS releases ownership on process death. Coordinates this PC only.
        """
        with file_lock(self.lock_path(isrc), timeout=timeout):
            yield

    def candidates(self, isrc):
        isrc = str(isrc or '').strip().upper()
        if not isrc:
            return []
        with self._mutex:
            paths = [r[0] for r in self._conn.execute(
                'SELECT path FROM files WHERE isrc=? ORDER BY path', (isrc,)).fetchall()]
        result = []
        for path in paths:
            evidence = inspect_audio(path, self.root)
            if evidence and evidence.isrc == isrc:
                result.append(evidence)
        return sorted(result, key=lambda e: (e.rank, -len(e.path), e.path), reverse=True)

    def find(self, isrc):
        candidates = self.candidates(isrc)
        return candidates[0].path if candidates else None

    def add(self, isrc, path):
        """Register only stable completed audio with the expected on-disk tag."""
        evidence = inspect_audio(path, self.root)
        if evidence is None or evidence.isrc != str(isrc or '').strip().upper():
            return False
        with self._mutex, self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO files(path,mtime,size,isrc) VALUES(?,?,?,?)',
                (evidence.path, evidence.mtime_ns / 1e9, evidence.size, evidence.isrc))
        return True

    def remove(self, path):
        with self._mutex, self._conn:
            self._conn.execute('DELETE FROM files WHERE path=?', (os.path.abspath(path),))

    def duplicates(self):
        with self._mutex:
            rows = self._conn.execute(
                "SELECT isrc,path FROM files WHERE isrc IN "
                "(SELECT isrc FROM files WHERE isrc IS NOT NULL AND isrc != '' "
                "GROUP BY isrc HAVING count(*) > 1) ORDER BY isrc,path").fetchall()
        groups = {}
        for isrc, path in rows:
            # Cheap lexical filter only; candidate inspection resolves links in
            # parallel. A cached report never authorizes filesystem mutations.
            try:
                root = os.path.normcase(os.path.abspath(self.root))
                normalized = os.path.normcase(os.path.abspath(path))
                rel = os.path.relpath(normalized, root)
                if os.path.commonpath((normalized, root)) == root and '_duplicados' not in rel.lower().split(os.sep):
                    groups.setdefault(isrc, []).append(path)
            except ValueError:
                continue
        return {k: v for k, v in groups.items() if len(v) > 1}

    def close(self):
        with self._mutex:
            self._conn.close()
