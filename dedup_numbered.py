"""Remove " (2)"/" (3)"... numbered duplicate audio files that are the SAME
recording (same ISRC) as the un-numbered original in the same folder.

    python dedup_numbered.py --dir "Z:\\"          # DRY-RUN (lista, no borra)
    python dedup_numbered.py --dir "Z:\\" --apply  # borra los duplicados

Only deletes a "<name> (N).ext" when "<name>.ext" exists in the same folder AND
both carry the same non-empty ISRC. Files without a readable ISRC are left alone.
"""
import os, re, argparse
from mutagen import File as MutFile

ap = argparse.ArgumentParser()
ap.add_argument('--dir', default='Z:\\')
ap.add_argument('--apply', action='store_true')
args = ap.parse_args()

AUDIO_EXT = {'.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav'}
NUM_RE = re.compile(r'^(?P<base>.+) \(\d+\)(?P<ext>\.[^.]+)$')


def isrc_of(path):
    try:
        mf = MutFile(path)
        tags = getattr(mf, 'tags', None)
        if not tags:
            return None
        for k in ('isrc', 'ISRC', 'TSRC', '----:com.apple.iTunes:ISRC'):
            if k in tags:
                v = tags[k]
                v = v[0] if isinstance(v, (list, tuple)) and v else v
                if hasattr(v, 'text'):
                    t = v.text
                    v = t[0] if isinstance(t, (list, tuple)) and t else t
                if isinstance(v, bytes):
                    v = v.decode('utf-8', 'ignore')
                v = str(v).strip().upper()
                if v:
                    return v
    except Exception:
        return None
    return None


count = removed = freed = 0
for dp, _dirs, files in os.walk(args.dir):
    for f in files:
        if os.path.splitext(f)[1].lower() not in AUDIO_EXT:
            continue
        m = NUM_RE.match(f)
        if not m:
            continue
        numbered = os.path.join(dp, f)
        base = os.path.join(dp, m.group('base') + m.group('ext'))
        if not os.path.isfile(base):
            continue  # no un-numbered original -> keep (don't touch)
        bi = isrc_of(base)
        ni = isrc_of(numbered)
        if not (bi and ni and bi == ni):
            continue  # only delete confirmed same-recording duplicates
        sz = os.path.getsize(numbered)
        count += 1
        freed += sz
        print(("DEL " if args.apply else "DRY ") + numbered)
        if args.apply:
            try:
                os.remove(numbered)
                # remove sidecar .lrc if present
                lrc = os.path.splitext(numbered)[0] + '.lrc'
                if os.path.isfile(lrc):
                    os.remove(lrc)
                removed += 1
            except OSError as e:
                print("  no se pudo borrar:", e)

print(f"\n{'BORRADOS' if args.apply else 'CANDIDATOS'}: {removed if args.apply else count}"
      f" | espacio {'liberado' if args.apply else 'recuperable'}: {freed/1024/1024:.1f} MB")
