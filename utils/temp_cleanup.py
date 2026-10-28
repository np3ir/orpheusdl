"""Keep temp/ down to what is needed.

orpheus gives every process its own temp/<pid> folder and removes it when the run ends
normally. A run that is stopped or killed never gets there, so the folders piled up.
sweep_stale_temp() removes the folders of processes that no longer exist; folders of live
processes and the staging area are never touched.
"""
import os
import shutil

KEEP = {'staging'}


def _pid_alive(pid):
    from utils.stage_publish import pid_alive
    return pid_alive(pid)


def sweep_stale_temp(temp_root='temp', alive=None):
    """Remove temp/<pid> folders whose process is gone. Returns the number removed."""
    alive = alive or _pid_alive
    removed = 0
    try:
        names = os.listdir(temp_root)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(temp_root, name)
        if name in KEEP or not name.isdigit() or not os.path.isdir(path):
            continue
        if alive(name):
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed += 1
    return removed
