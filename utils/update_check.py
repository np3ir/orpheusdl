"""Tell the user when the installed checkout is behind GitHub.

Called at the start of abq and orpheus.py. Read-only: compares the local HEAD
with the upstream branch on the remote via ``git ls-remote`` (no fetch, no pull,
nothing in the working tree changes). The remote answer is cached for a few hours
in config/update_check.json so runs don't hit GitHub every time. Never raises and
never blocks for long: no git, no .git folder, no upstream, no network -> silent.

Set ORPHEUS_NO_UPDATE_CHECK=1 to disable. Child processes (abq -> orpheus.py ->
isrc_recover.py) inherit ORPHEUS_UPDATE_CHECKED and don't repeat the check.
"""
import json
import os
import shutil
import subprocess
import time

_CACHE_HOURS = 6
_TIMEOUT = 8   # seconds for the ls-remote call


def _git(repo, *args, timeout=5):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout.strip()


def check(repo_dir, config_dir, now=None):
    """Return (local_sha, remote_sha, upstream) when the checkout is behind its
    upstream, else None."""
    if not (shutil.which('git') and os.path.exists(os.path.join(repo_dir, '.git'))):
        return None
    rc, local = _git(repo_dir, 'rev-parse', 'HEAD')
    if rc or not local:
        return None
    rc, upstream = _git(repo_dir, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
    if rc or '/' not in upstream:
        return None                      # detached HEAD / branch without upstream
    remote, branch = upstream.split('/', 1)

    now = time.time() if now is None else now
    cache_path = os.path.join(config_dir, 'update_check.json')
    try:
        with open(cache_path, encoding='utf-8') as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    if (cache.get('upstream') == upstream and cache.get('remote_sha')
            and now - float(cache.get('checked') or 0) < _CACHE_HOURS * 3600):
        remote_sha = cache['remote_sha']
    else:
        rc, out = _git(repo_dir, 'ls-remote', remote, f'refs/heads/{branch}', timeout=_TIMEOUT)
        remote_sha = out.split()[0] if (rc == 0 and out) else ''
        if not remote_sha:
            return None
        try:
            os.makedirs(config_dir, exist_ok=True)
            tmp = f'{cache_path}.{os.getpid()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'checked': now, 'upstream': upstream, 'remote_sha': remote_sha}, f)
            os.replace(tmp, cache_path)
        except Exception:
            pass

    if remote_sha == local:
        return None
    # Remote commit already in local history -> local is ahead (unpushed work), not behind.
    rc, _ = _git(repo_dir, 'merge-base', '--is-ancestor', remote_sha, local)
    if rc == 0:
        return None
    return local, remote_sha, upstream


def notify_if_outdated(repo_dir, config_dir, print_fn=print):
    """Print an update notice when the checkout is behind GitHub. Never raises."""
    if os.environ.get('ORPHEUS_NO_UPDATE_CHECK') or os.environ.get('ORPHEUS_UPDATE_CHECKED'):
        return
    os.environ['ORPHEUS_UPDATE_CHECKED'] = '1'   # inherited by child processes
    try:
        found = check(repo_dir, config_dir)
    except Exception:
        return
    if not found:
        return
    local, remote_sha, upstream = found
    print_fn(f'\n*** Update available / Actualizacion disponible: {upstream} is at {remote_sha[:7]}, '
             f'installed {local[:7]}.\n'
             f'*** To update / Para actualizar:  git -C "{repo_dir}" pull\n')
