"""Validate installation settings; set lossless defaults only on a NEW install."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile


def configure(path, new=False):
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    general = data['global']['general']
    if new:
        general['download_quality'] = 'lossless'
        data['global']['codecs']['flac_only'] = True
        # Atomic update; never copy another computer's credentials or paths.
        shutil.copy2(path, path.with_name(path.name + '.bak'))
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.install-', suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, indent=4, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    quality = str(general.get('download_quality', '')).upper()
    allowed = {'MINIMUM', 'LOW', 'MEDIUM', 'HIGH', 'LOSSLESS', 'HIFI', 'ATMOS'}
    if quality not in allowed:
        raise ValueError('global.general.download_quality must be lossless, hifi, high, medium, low, minimum or atmos. Check spelling: LOSELESS is invalid.')
    if not isinstance(data['global']['codecs'].get('flac_only', True), bool):
        raise ValueError('global.codecs.flac_only must be true or false, without quotes.')
    if not isinstance(data.get('modules'), dict):
        raise ValueError('Module settings are missing; run orpheus.py settings refresh.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    parser.add_argument('--new', action='store_true')
    args = parser.parse_args()
    try:
        configure(args.path, args.new)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, 'Settings check failed: ' + str(exc) + '\n')
    print('Settings validated. Account credentials remain local.')
