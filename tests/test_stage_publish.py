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
