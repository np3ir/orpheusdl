"""Rename album folders "(YYYY) Name" -> "Name" so the same album from different
services (which report different years) lands in ONE folder. On a collision the
source folder is MERGED into the target: files move in; a same-name file that is
the same recording (same ISRC, else same size) is dropped as a duplicate, and a
genuinely different one is kept as " (N)".

    python rename_strip_year.py --dir "Z:\\"           # DRY-RUN (no cambia nada)
    python rename_strip_year.py --dir "Z:\\" --apply   # aplica
"""
import os, re, argparse, shutil
from mutagen import File as MutFile

ap = argparse.ArgumentParser()
ap.add_argument('--dir', default='Z:\\')
ap.add_argument('--apply', action='store_true')
args = ap.parse_args()

YEAR_RE = re.compile(r'^\((\d{4})\) (.+)$')
AUDIO_EXT = {'.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav'}


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


# 1. collect all "(YYYY) Name" folders first (never rename during the walk)
targets = []
for dp, dirs, _files in os.walk(args.dir):
    for d in dirs:
        m = YEAR_RE.match(d)
        if m:
            targets.append((os.path.join(dp, d), os.path.join(dp, m.group(2))))

renames = merges = files_moved = dups_dropped = kept_variants = 0
for src, dst in targets:
    if not os.path.isdir(src):
        continue
    if not os.path.exists(dst):
        renames += 1
        print(("MV    " if args.apply else "DRY-MV    ") + src + "  ->  " + os.path.basename(dst))
        if args.apply:
            os.rename(src, dst)
    else:
        merges += 1
        print(("MERGE " if args.apply else "DRY-MERGE ") + src + "  ->  " + os.path.basename(dst))
        if args.apply:
            for root, _d, fs in os.walk(src):
                rel = os.path.relpath(root, src)
                tdir = dst if rel == '.' else os.path.join(dst, rel)
                os.makedirs(tdir, exist_ok=True)
                for f in fs:
                    sfp, tfp = os.path.join(root, f), os.path.join(tdir, f)
                    if not os.path.exists(tfp):
                        shutil.move(sfp, tfp); files_moved += 1
                        continue
                    same = os.path.getsize(sfp) == os.path.getsize(tfp)
                    if os.path.splitext(f)[1].lower() in AUDIO_EXT:
                        si, ti = isrc_of(sfp), isrc_of(tfp)
                        if si and ti:
                            same = (si == ti)
                    if same:
                        os.remove(sfp); dups_dropped += 1
                    else:
                        base, ext = os.path.splitext(tfp); n = 2
                        while os.path.exists(f'{base} ({n}){ext}'):
                            n += 1
                        shutil.move(sfp, f'{base} ({n}){ext}'); kept_variants += 1
            shutil.rmtree(src, ignore_errors=True)

print(f"\n{'APLICADO' if args.apply else 'DRY-RUN'}: renombrar={renames} | fusionar(colisiones)={merges}"
      + (f" | archivos movidos={files_moved} | dups descartados={dups_dropped} | variantes={kept_variants}" if args.apply else ""))
