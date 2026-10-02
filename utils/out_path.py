"""Normalize a user-supplied download path (``-o`` / settings download_path)."""
import re

_DRIVE_ONLY = re.compile(r'^[A-Za-z]:$')


def normalize_out_path(path):
    """Trailing separators removed, but a drive root stays a root.

    On Windows ``Z:`` (no backslash) means "the current directory on drive Z",
    not its root, so ``"Z:\\"`` must not be trimmed to ``Z:`` and a bare ``Z:`` is
    turned into ``Z:\\``. Also drops a stray trailing ``"``: in cmd/PowerShell
    ``-o "Z:\\"`` reaches Python as ``Z:"`` (the backslash escapes the quote), and
    ``"`` can never be part of a Windows path.
    """
    if not path:
        return path
    p = str(path).strip().rstrip('"').strip()
    trimmed = p.rstrip('/\\')
    if _DRIVE_ONLY.match(trimmed):
        return trimmed + '\\'
    return trimmed or p   # '/' stays '/'
