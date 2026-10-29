import os

import pytest

from utils import stage_publish as sp


def write(path, data=b'audio-bytes'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def test_resolve_stage_dir_rules(tmp_path):
    proj = str(tmp_path)
    net = lambda p: True       # noqa: E731
    loc = lambda p: False      # noqa: E731
    out = r'Z:\Music'
    assert sp.resolve_stage_dir({}, out, no_stage=True, project_dir=proj, network_check=net) is None
    assert sp.resolve_stage_dir({}, '', project_dir=proj, network_check=net) is None
    assert sp.resolve_stage_dir({}, out, project_dir=proj, network_check=loc) is None        # local destination
    assert sp.resolve_stage_dir({}, out, project_dir=proj, network_check=net) == os.path.join(proj, 'temp', 'staging')
    cfg = {'global': {'general': {'staging_dir': str(tmp_path / 'fast')}}}
    assert sp.resolve_stage_dir(cfg, out, project_dir=proj, network_check=net) == str(tmp_path / 'fast')
    off = {'global': {'general': {'stage_network_downloads': False}}}
    assert sp.resolve_stage_dir(off, out, project_dir=proj, network_check=net) is None
    forced = sp.resolve_stage_dir(off, out, stage_dir_arg=str(tmp_path / 'x'), project_dir=proj, network_check=loc)
    assert forced == str(tmp_path / 'x')                                                      # explicit dir forces it


def test_is_network_path_unc_and_local():
    bs = chr(92)
    assert sp.is_network_path(bs * 2 + 'nas' + bs + 'music' + bs + 'a')
    assert not sp.is_network_path(os.path.abspath(__file__))


def test_copy_verified_ok_and_leaves_no_temp(tmp_path):
    src = write(str(tmp_path / 's' / 'a.flac'), b'x' * 3_000_000)
    dst = str(tmp_path / 'd' / 'A' / 'a.flac')
    assert sp.copy_verified(src, dst) == dst
    assert open(dst, 'rb').read() == open(src, 'rb').read()
    assert [n for n in os.listdir(os.path.dirname(dst)) if n.startswith('.orpheus-')] == []


def test_copy_verified_detects_corruption(tmp_path, monkeypatch):
    src = write(str(tmp_path / 's' / 'a.flac'))
    dst = str(tmp_path / 'd' / 'a.flac')
    monkeypatch.setattr(sp, '_sha256', lambda p: 'bad')          # what landed does not match
    with pytest.raises(OSError):
        sp.copy_verified(src, dst)
    assert not os.path.exists(dst) and os.listdir(os.path.dirname(dst)) == []


def test_publish_dir_moves_keeps_structure_skips_parts(tmp_path):
    s, d = str(tmp_path / 'stage'), str(tmp_path / 'lib')
    write(os.path.join(s, 'A', 'Artist', '(2020) Alb', '01-01. T.flac'))
    write(os.path.join(s, 'A', 'Artist', '(2020) Alb', '.orpheus-xyz.part.flac'), b'partial')
    stats = sp.publish_dir(s, d, index=None, log=lambda *a: None)
    assert stats['moved'] == 1 and not stats['failed']
    assert os.path.exists(os.path.join(d, 'A', 'Artist', '(2020) Alb', '01-01. T.flac'))
    assert os.path.exists(os.path.join(s, 'A', 'Artist', '(2020) Alb', '.orpheus-xyz.part.flac'))   # untouched
    assert not os.path.exists(os.path.join(s, 'A', 'Artist', '(2020) Alb', '01-01. T.flac'))


def test_publish_dir_never_overwrites(tmp_path):
    s, d = str(tmp_path / 'stage'), str(tmp_path / 'lib')
    write(os.path.join(d, 'A', 'x.flac'), b'same')
    write(os.path.join(s, 'A', 'x.flac'), b'same')
    st = sp.publish_dir(s, d, log=lambda *a: None)
    assert st['reused'] == 1 and st['moved'] == 0 and not os.path.exists(os.path.join(s, 'A', 'x.flac'))
    write(os.path.join(s, 'A', 'x.flac'), b'DIFFERENT')
    st = sp.publish_dir(s, d, log=lambda *a: None)
    assert st['moved'] == 1
    assert open(os.path.join(d, 'A', 'x.flac'), 'rb').read() == b'same'
    assert open(os.path.join(d, 'A', 'x (2).flac'), 'rb').read() == b'DIFFERENT'


def test_publish_dir_failure_keeps_staged_file(tmp_path):
    s, d = str(tmp_path / 'stage'), str(tmp_path / 'lib')
    write(os.path.join(s, 'A', 'x.flac'))

    def boom(src, dst):
        raise OSError('NAS went away')
    st = sp.publish_dir(s, d, log=lambda *a: None, copy=boom)
    assert st['moved'] == 0 and len(st['failed']) == 1
    assert os.path.exists(os.path.join(s, 'A', 'x.flac'))


def test_stage_lifecycle_and_cleanup(tmp_path):
    lib, cfg = str(tmp_path / 'lib'), str(tmp_path / 'cfg')
    os.makedirs(lib)
    stg = sp.Stage(str(tmp_path / 'staging'), lib, cfg, log=lambda *a: None, open_index=lambda: None)
    assert os.path.exists(os.path.join(stg.run_dir, 'run.pid'))
    write(os.path.join(stg.run_dir, 'B', 'b.flac'))
    assert stg.publish()['moved'] == 1
    assert os.path.exists(os.path.join(lib, 'B', 'b.flac'))
    write(os.path.join(stg.run_dir, 'C', 'c.flac'))
    stats = stg.finish()
    assert stats['moved'] == 2 and not os.path.exists(stg.run_dir)


def test_finish_keeps_undelivered_and_recovery_delivers_it(tmp_path, monkeypatch):
    lib, cfg = str(tmp_path / 'lib'), str(tmp_path / 'cfg')
    os.makedirs(lib)
    quiet = lambda *a: None      # noqa: E731
    stg = sp.Stage(str(tmp_path / 'staging'), lib, cfg, log=quiet, open_index=lambda: None)
    write(os.path.join(stg.run_dir, 'B', 'b.flac'))
    monkeypatch.setattr(sp, 'copy_verified', lambda s, d: (_ for _ in ()).throw(OSError('offline')))
    stg.finish()
    assert os.path.exists(os.path.join(stg.run_dir, 'B', 'b.flac'))             # kept for retry
    assert not os.path.exists(os.path.join(stg.run_dir, 'run.pid'))             # marked as a dead run
    monkeypatch.undo()
    n = sp.recover_leftovers(str(tmp_path / 'staging'), lib, cfg, log=quiet, open_index=lambda: None)
    assert n == 1 and os.path.exists(os.path.join(lib, 'B', 'b.flac'))
    assert not os.path.exists(stg.run_dir)


def test_recovery_never_touches_a_live_run(tmp_path):
    lib, cfg = str(tmp_path / 'lib'), str(tmp_path / 'cfg')
    os.makedirs(lib)
    live = tmp_path / 'staging' / 'run-live'
    write(str(live / 'A' / 'a.flac'))
    (live / 'run.pid').write_text(str(os.getpid()))
    dead = tmp_path / 'staging' / 'run-dead'
    write(str(dead / 'A' / 'd.flac'))
    (dead / 'run.pid').write_text('999999999')
    n = sp.recover_leftovers(str(tmp_path / 'staging'), lib, cfg, log=lambda *a: None, open_index=lambda: None)
    assert n == 1
    assert (live / 'A' / 'a.flac').exists() and not (dead / 'A' / 'd.flac').exists()
    assert os.path.exists(os.path.join(lib, 'A', 'd.flac')) and not os.path.exists(os.path.join(lib, 'A', 'a.flac'))


def test_pid_alive():
    assert sp.pid_alive(os.getpid())
    assert not sp.pid_alive(999999999) and not sp.pid_alive('x') and not sp.pid_alive(0)


def test_name_budget_uses_the_library_path_not_the_staging_path(monkeypatch):
    from types import SimpleNamespace
    from orpheus.music_downloader import Downloader as Downloader
    bs = chr(92)
    stage = 'C:' + bs + 'o' + bs + 'temp' + bs + 'staging' + bs + 'abc'
    lib = 'Z:' + bs
    rel = bs + 'A' + bs + 'x.flac'
    me = SimpleNamespace(path=stage)
    monkeypatch.delenv('ORPHEUS_DEDUP_ROOT', raising=False)
    assert Downloader._projected_final_length(me, stage + rel) == len(stage + rel)
    monkeypatch.setenv('ORPHEUS_DEDUP_ROOT', lib)
    assert Downloader._projected_final_length(me, stage + rel) == len('Z:' + rel)       # sized for Z:, not for staging
    assert Downloader._projected_final_length(me, 'D:' + bs + 'elsewhere.flac') == len('D:' + bs + 'elsewhere.flac')


def test_sweep_stale_temp_keeps_live_and_staging(tmp_path):
    from utils.temp_cleanup import sweep_stale_temp
    root = tmp_path / 'temp'
    for name in ('111', '222', 'staging', 'notapid'):
        (root / name).mkdir(parents=True)
    (root / '111' / 'cover-file').write_bytes(b'x')       # dead process, has a leftover file
    (root / 'loose.txt').write_text('keep')
    removed = sweep_stale_temp(str(root), alive=lambda pid: pid == '222')
    left = sorted(p.name for p in root.iterdir())
    assert removed == 1 and left == ['222', 'loose.txt', 'notapid', 'staging']


class FakeIndex:
    """Stands in for the library ISRC index: remembers delivered recordings, serialises per ISRC."""
    def __init__(self):
        self.have = {}          # isrc -> evidence
        self.claims = []
        self.added = []

    def candidates(self, isrc):
        return [self.have[isrc]] if isrc in self.have else []

    def claim(self, isrc, timeout=3600):
        import contextlib
        self.claims.append(isrc)
        return contextlib.nullcontext()

    def add(self, isrc, path):
        self.added.append((isrc, path))
        from types import SimpleNamespace
        self.have[isrc] = SimpleNamespace(isrc=isrc, channels=2, duration=200.0)
        return True


def test_two_runs_delivering_the_same_recording_leave_one_copy(tmp_path):
    from types import SimpleNamespace
    ev = lambda p: SimpleNamespace(isrc='ISRC00000001', channels=2, duration=200.0)   # noqa: E731
    lib, index = str(tmp_path / 'lib'), FakeIndex()
    a, b = str(tmp_path / 'stageA'), str(tmp_path / 'stageB')
    write(os.path.join(a, 'A', 'Artist', '(2020) Alb', '01-01. T.flac'), b'download-from-run-A')
    write(os.path.join(b, 'A', 'Artist', '(2020) Single', '01-01. T.flac'), b'download-from-run-B')
    sa = sp.publish_dir(a, lib, index, log=lambda *x: None, evidence_of=ev)
    sb = sp.publish_dir(b, lib, index, log=lambda *x: None, evidence_of=ev)
    assert sa['moved'] == 1 and sb['moved'] == 0 and sb['skipped'] == 1
    delivered = [os.path.join(d, n) for d, _, ns in os.walk(lib) for n in ns]
    assert len(delivered) == 1 and ' (2)' not in delivered[0]
    assert not os.path.exists(os.path.join(b, 'A', 'Artist', '(2020) Single', '01-01. T.flac'))   # staged copy dropped
    assert index.claims == ['ISRC00000001', 'ISRC00000001']                                       # delivered under the ISRC claim


def test_a_different_duration_is_not_treated_as_the_same_recording(tmp_path):
    from types import SimpleNamespace
    lib, index = str(tmp_path / 'lib'), FakeIndex()
    index.have['ISRC00000002'] = SimpleNamespace(isrc='ISRC00000002', channels=2, duration=100.0)
    s = str(tmp_path / 'stage')
    write(os.path.join(s, 'X', 'x.flac'), b'long version')
    ev = lambda p: SimpleNamespace(isrc='ISRC00000002', channels=2, duration=250.0)   # noqa: E731
    st = sp.publish_dir(s, lib, index, log=lambda *x: None, evidence_of=ev)
    assert st['moved'] == 1 and st['skipped'] == 0


def test_handle_limit_is_retried_then_delivered(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(sp.time, 'sleep', lambda s: sleeps.append(s))
    s, d = str(tmp_path / 'stage'), str(tmp_path / 'lib')
    write(os.path.join(s, 'A', 'x.flac'))
    calls = {'n': 0}
    real = sp.copy_verified

    def flaky(src, dst):
        calls['n'] += 1
        if calls['n'] <= 2:
            raise OSError(24, 'Too many open files')
        return real(src, dst)
    st = sp.publish_dir(s, d, log=lambda *a: None, copy=flaky)
    assert st['moved'] == 1 and not st['failed'] and calls['n'] == 3
    assert sleeps[:2] == [3, 8] and os.path.exists(os.path.join(d, 'A', 'x.flac'))


def test_handle_limit_that_never_clears_keeps_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.time, 'sleep', lambda s: None)
    s, d = str(tmp_path / 'stage'), str(tmp_path / 'lib')
    write(os.path.join(s, 'A', 'x.flac'))

    def always(src, dst):
        e = OSError('The system cannot open the file')
        e.winerror = 4
        raise e
    st = sp.publish_dir(s, d, log=lambda *a: None, copy=always)
    assert st['moved'] == 0 and len(st['failed']) == 1 and os.path.exists(os.path.join(s, 'A', 'x.flac'))


def test_other_errors_are_not_retried(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(sp.time, 'sleep', lambda s: sleeps.append(s))
    s, d = str(tmp_path / 'stage'), str(tmp_path / 'lib')
    write(os.path.join(s, 'A', 'x.flac'))

    def denied(src, dst):
        raise PermissionError(13, 'denied')
    st = sp.publish_dir(s, d, log=lambda *a: None, copy=denied)
    assert len(st['failed']) == 1 and sleeps == []


def test_failed_copy_cleans_its_temp_file_or_remembers_it(tmp_path, monkeypatch):
    src = write(str(tmp_path / 's' / 'a.flac'))
    dst = str(tmp_path / 'd' / 'a.flac')
    monkeypatch.setattr(sp, '_sha256', lambda p: 'bad')
    with pytest.raises(OSError):
        sp.copy_verified(src, dst)
    assert os.listdir(os.path.dirname(dst)) == [] and not sp._ORPHAN_PARTS
    # if the temp file cannot be removed, it is remembered and removed by the next sweep
    monkeypatch.setattr(sp, '_remove_quietly', lambda p, tries=3: False)
    with pytest.raises(OSError):
        sp.copy_verified(src, dst)
    left = list(sp._ORPHAN_PARTS)
    assert len(left) == 1 and os.path.exists(left[0])
    monkeypatch.undo()
    sp.sweep_orphan_parts()
    assert not os.path.exists(left[0]) and not sp._ORPHAN_PARTS


def test_delivery_lock_makes_a_second_delivery_wait(tmp_path):
    import threading
    import time as _time
    lib = str(tmp_path / 'lib')
    os.makedirs(lib)
    quiet = lambda *x: None      # noqa: E731
    a = sp.Stage(str(tmp_path / 'staging'), lib, str(tmp_path / 'cfg'), log=quiet, open_index=lambda: None)
    b = sp.Stage(str(tmp_path / 'staging'), lib, str(tmp_path / 'cfg'), log=quiet, open_index=lambda: None)
    write(os.path.join(b.run_dir, 'B', 'b.flac'))
    done = threading.Event()
    with a._delivery_lock():                       # instance A is delivering
        th = threading.Thread(target=lambda: (b.publish(), done.set()))
        th.start()
        _time.sleep(0.6)
        assert not done.is_set() and not os.path.exists(os.path.join(lib, 'B', 'b.flac'))   # B waits
    th.join(timeout=10)
    assert done.is_set() and os.path.exists(os.path.join(lib, 'B', 'b.flac'))               # then delivers


def test_delivery_cleans_temp_parts_of_dead_runs_only(tmp_path):
    lib, s = str(tmp_path / 'lib'), str(tmp_path / 'stage')
    folder = os.path.join(lib, 'A', 'Artist')
    dead = write(os.path.join(folder, '.orpheus-pub-999999999-123.part'), b'junk')
    live = write(os.path.join(folder, f'.orpheus-pub-{os.getpid()}-456.part'), b'in progress')
    other = write(os.path.join(folder, '.orpheus-abc.part.flac'), b'orpheus own temp')
    write(os.path.join(s, 'A', 'Artist', 'x.flac'))
    sp.publish_dir(s, lib, log=lambda *a: None)
    assert not os.path.exists(dead) and os.path.exists(live) and os.path.exists(other)
    assert os.path.exists(os.path.join(folder, 'x.flac'))


def test_settings_replace_retries_on_permission_error(monkeypatch, tmp_path):
    from orpheus import core
    calls = {'n': 0}
    real = core.os.replace

    def flaky(a, b):
        calls['n'] += 1
        if calls['n'] < 3:
            raise PermissionError(5, 'Access denied')
        return real(a, b)
    monkeypatch.setattr(core.os, 'replace', flaky)
    monkeypatch.setattr(core.time, 'sleep', lambda s: None)
    target = tmp_path / 's.json'
    core._atomic_write_text(str(target), '{"a": 1}')
    assert target.read_text() == '{"a": 1}' and calls['n'] == 3
