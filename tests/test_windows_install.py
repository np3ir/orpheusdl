"""Offline installer/launcher checks. Never change the user's persistent PATH."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('configure_install', ROOT / 'scripts/configure_install.py')
configurer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configurer)


@pytest.fixture
def settings_file(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'global': {'general': {'download_quality': 'hifi', 'download_path': 'custom'},
                                         'codecs': {'flac_only': False}},
                                'modules': {'demo': {'token': 'synthetic-test-token'}}}), encoding='utf-8')
    return path


def test_existing_configuration_is_not_rewritten(settings_file):
    before = settings_file.read_bytes()
    configurer.configure(settings_file)
    assert settings_file.read_bytes() == before
    assert not settings_file.with_suffix('.json.bak').exists()


def test_new_defaults_are_lossless_with_backup(settings_file):
    before = settings_file.read_bytes()
    configurer.configure(settings_file, new=True)
    data = json.loads(settings_file.read_text(encoding='utf-8'))
    assert data['global']['general']['download_quality'] == 'lossless'
    assert data['global']['codecs']['flac_only'] is True
    assert data['global']['general']['download_path'] == 'custom'
    assert data['modules']['demo']['token'] == 'synthetic-test-token'
    assert settings_file.with_suffix('.json.bak').read_bytes() == before


def test_bad_quality_has_clear_error_without_reset(settings_file):
    data = json.loads(settings_file.read_text(encoding='utf-8'))
    data['global']['general']['download_quality'] = 'LOSELESS'
    settings_file.write_text(json.dumps(data), encoding='utf-8')
    before = settings_file.read_bytes()
    with pytest.raises(ValueError, match='LOSELESS'):
        configurer.configure(settings_file)
    assert settings_file.read_bytes() == before


@pytest.fixture(scope='module')
def windows_install(tmp_path_factory):
    if os.name != 'nt': pytest.skip('Windows launcher')
    root = tmp_path_factory.mktemp('installation with spaces')
    venv.EnvBuilder(with_pip=False).create(root / '.venv')
    shutil.copy2(ROOT / 'orpheus.cmd', root / 'orpheus.cmd')
    shutil.copy2(ROOT / 'install.ps1', root / 'install.ps1')
    (root / 'orpheus.py').write_text(
        'import sys,os,json\nprint(json.dumps({"argv":sys.argv[1:],"python":sys.executable,"cwd":os.getcwd()}))\n'
        'sys.exit(17 if "--failure" in sys.argv else 0)\n',encoding='utf-8')
    return root


def test_launcher_handles_spaces_url_arguments_and_arbitrary_cwd(windows_install, tmp_path):
    launcher = windows_install / 'orpheus.cmd'
    url = 'https://example.test/track/123?one=1&two=2'
    result = subprocess.run(f'cmd.exe /d /s /c ""{launcher}" "{url}" -o "D:/Music Test""',
                            cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout
    output = json.loads(result.stdout)
    assert output['argv'] == [url,'-o','D:/Music Test']
    assert Path(output['python']) == windows_install / '.venv/Scripts/python.exe'
    assert Path(output['cwd']) == windows_install


def test_launcher_preserves_exit_code(windows_install, tmp_path):
    result = subprocess.run(f'cmd.exe /d /s /c ""{windows_install / "orpheus.cmd"}" --failure"',
                            cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert result.returncode == 17, result.stderr + result.stdout


def powershell(code):
    exe = shutil.which('powershell.exe') or shutil.which('pwsh')
    if not exe: pytest.skip('PowerShell unavailable')
    return subprocess.run([exe,'-NoProfile','-ExecutionPolicy','Bypass','-Command',code],
                          capture_output=True,text=True,timeout=30)


def test_path_merge_preserves_other_entries_and_is_idempotent(windows_install):
    script = str(windows_install / 'install.ps1').replace("'", "''")
    result = powershell(f". '{script}'; $first=Add-OrpheusPathEntry 'C:\\Other;C:\\Install;D:\\Tools' 'C:\\Install'; "
                        "$second=Add-OrpheusPathEntry $first 'C:\\Install'; "
                        "@{first=$first;second=$second}|ConvertTo-Json -Compress")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['first'] == output['second']
    assert output['first'].split(';') == [r'C:\Install',r'C:\Other',r'D:\Tools']


def test_register_command_only_is_transient_in_tests(windows_install):
    script = str(windows_install / 'install.ps1').replace("'", "''")
    result = powershell(
        "$before=[Environment]::GetEnvironmentVariable('Path','User'); "
        f"& '{script}' -RegisterCommandOnly -NoUserPath; "
        "if ($before -cne [Environment]::GetEnvironmentVariable('Path','User')) {throw 'User PATH changed'}; "
        "(Get-Command orpheus).Source")
    assert result.returncode == 0, result.stderr + result.stdout
    assert str(windows_install / 'orpheus.cmd') in result.stdout
    report = json.loads((windows_install / 'config/install-report.json').read_text(encoding='utf-8-sig'))
    assert report['user_path_updated'] is False
    assert report['mode'] == 'register-command'
