"""Tests for utils/update_check.py with real local git repos (no network)."""
import os
import shutil
import subprocess

import pytest

import utils.update_check as uc

pytestmark = pytest.mark.skipif(not shutil.which('git'), reason='git not installed')


def git(cwd, *args):
    subprocess.run(['git', '-C', str(cwd), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    '-c', 'commit.gpgsign=false', *args], check=True, capture_output=True)


def commit(repo, name):
    (repo / name).write_text(name)
    git(repo, 'add', name)
    git(repo, 'commit', '-q', '-m', name)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.delenv('ORPHEUS_NO_UPDATE_CHECK', raising=False)
    monkeypatch.delenv('ORPHEUS_UPDATE_CHECKED', raising=False)
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '-q', '--bare', '-b', 'main', str(remote))
    dev = tmp_path / 'dev'
    git(tmp_path, 'clone', '-q', str(remote), str(dev))
    git(dev, 'checkout', '-q', '-b', 'main')
    commit(dev, 'a')
    git(dev, 'push', '-q', 'origin', 'main')
    install = tmp_path / 'install'
    git(tmp_path, 'clone', '-q', str(remote), str(install))
    return dev, install, tmp_path / 'config'


def notices(install, config):
    out = []
    os.environ.pop('ORPHEUS_UPDATE_CHECKED', None)
    uc.notify_if_outdated(str(install), str(config), print_fn=out.append)
    return out


def test_up_to_date_is_silent(repos):
    dev, install, config = repos
    assert notices(install, config) == []


def test_behind_prints_how_to_update(repos):
    dev, install, config = repos
    commit(dev, 'b')
    git(dev, 'push', '-q', 'origin', 'main')
    out = notices(install, config)
    assert len(out) == 1
    assert 'Update available / Actualizacion disponible' in out[0]
    assert f'git -C "{install}" pull' in out[0]


def test_local_ahead_is_not_reported(repos):
    dev, install, config = repos
    commit(install, 'local-only')
    assert notices(install, config) == []


def test_remote_answer_is_cached(repos, monkeypatch):
    dev, install, config = repos
    assert notices(install, config) == []            # caches the current remote sha
    commit(dev, 'b')
    git(dev, 'push', '-q', 'origin', 'main')
    assert notices(install, config) == []            # within the cache window: no ls-remote
    real = uc.time.time
    monkeypatch.setattr(uc.time, 'time', lambda: real() + 7 * 3600)
    assert len(notices(install, config)) == 1        # cache expired -> sees the new commit


def test_disabled_by_env_and_checked_once_per_process_tree(repos, monkeypatch):
    dev, install, config = repos
    commit(dev, 'b')
    git(dev, 'push', '-q', 'origin', 'main')
    monkeypatch.setenv('ORPHEUS_NO_UPDATE_CHECK', '1')
    out = []
    uc.notify_if_outdated(str(install), str(config), print_fn=out.append)
    assert out == []
    monkeypatch.delenv('ORPHEUS_NO_UPDATE_CHECK')
    monkeypatch.setenv('ORPHEUS_UPDATE_CHECKED', '1')  # set by the parent (abq -> orpheus.py)
    uc.notify_if_outdated(str(install), str(config), print_fn=out.append)
    assert out == []


def test_not_a_git_checkout_or_unreachable_remote_is_silent(repos, tmp_path):
    dev, install, config = repos
    assert notices(tmp_path / 'nothing-here', config) == []
    git(install, 'remote', 'set-url', 'origin', str(tmp_path / 'missing.git'))
    assert notices(install, tmp_path / 'other-config') == []
