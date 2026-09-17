"""One-time cleanup: merge EXISTING duplicate album folders left by past multi-service
downloads. The SAME album from Deezer/Tidal/Qobuz reports different years, so it landed
in separate folders:

    Artista/(2019) Album  +  Artista/(2020) Album   (mismo album, distinto servicio)

This merges them into ONE folder (the one with the MOST tracks is kept; ties -> earliest
year), moving tracks in and DE-DUPING by ISRC (same ISRC/size -> drop; genuinely different
-> kept as " (N)"). Genuinely different same-named albums (different ISRCs, e.g. Chayanne
1988 vs 1998) are matched by ISRC and LEFT ALONE. Folders with no readable ISRC are left
alone (conservative).

    python merge_dupe_albums.py --dir "Z:\\"           # DRY-RUN (no cambia nada)
    python merge_dupe_albums.py --dir "Z:\\" --apply    # aplica
"""
import os, argparse, shutil
from collections import defaultdict
from mutagen import File as MutFile

ap = argparse.ArgumentParser()
ap.add_argument('--dir', default='Z:\\')
ap.add_argument('--apply', action='store_true')
args = ap.parse_args()

AUDIO = {'.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav'}


def strip_year(name):
    if len(name) > 7 and name[0] == '(' and name[5] == ')' and name[6] == ' ' and name[1:5].isdigit():
        return name[7:], name[1:5]
    return name, None


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


def audio_files(folder):
    try:
        return [n for n in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, n)) and os.path.splitext(n)[1].lower() in AUDIO]
    except OSError:
        return []


def folder_isrcs(folder):
    return {i for n in audio_files(folder) if (i := isrc_of(os.path.join(folder, n)))}


# 1. collect album folders (dirs that directly contain audio), grouped by (parent, stripped name)
groups = defaultdict(list)
for dp, _dirs, files in os.walk(args.dir):
    if any(os.path.splitext(f)[1].lower() in AUDIO for f in files):
        base, _yr = strip_year(os.path.basename(dp))
        groups[(os.path.dirname(dp), base)].append(dp)

merged_groups = folders_removed = files_moved = dups_dropped = kept_variants = kept_separate = 0
for (parent, base), folders in groups.items():
    if len(folders) < 2:
        continue
    # cluster folders by ISRC overlap: same album vs different-same-name album
    info = [(f, folder_isrcs(f), len(audio_files(f))) for f in folders]
    clusters = []
    for f, isr, cnt in info:
        placed = False
        for cl in clusters:
            ri = cl['isrcs']
            _m = min(len(isr), len(ri)) if (isr and ri) else 0
            # 1-2 track albums (singles) need FULL ISRC overlap; larger keep 50%.
            if isr and ri and len(isr & ri) >= (_m if _m <= 2 else max(2, 0.5 * _m)):
                cl['members'].append((f, cnt)); cl['isrcs'] |= isr; placed = True
                break
        if not placed:
            clusters.append({'members': [(f, cnt)], 'isrcs': set(isr)})
    if len([c for c in clusters if len(c['members']) >= 1]) > 1:
        kept_separate += 1  # this name has >1 genuinely-different album
    for cl in clusters:
        mem = cl['members']
        if len(mem) < 2:
            continue
        mem.sort(key=lambda m: (-m[1], int(strip_year(os.path.basename(m[0]))[1] or 9999)))
        keeper = mem[0][0]
        sources = [m[0] for m in mem[1:]]
        merged_groups += 1
        print(("MERGE     " if args.apply else "DRY-MERGE ") + keeper + "  <-  " + " , ".join(os.path.basename(s) for s in sources))
        if not args.apply:
            continue
        for src in sources:
            for root, _d, fs in os.walk(src):
                rel = os.path.relpath(root, src)
                tdir = keeper if rel == '.' else os.path.join(keeper, rel)
                os.makedirs(tdir, exist_ok=True)
                for f in fs:
                    sfp, tfp = os.path.join(root, f), os.path.join(tdir, f)
                    ext = os.path.splitext(f)[1].lower()
                    if not os.path.exists(tfp):
                        shutil.move(sfp, tfp); files_moved += 1
                        continue
                    same = os.path.getsize(sfp) == os.path.getsize(tfp)
                    if ext in AUDIO:
                        si, ti = isrc_of(sfp), isrc_of(tfp)
                        if si and ti:
                            same = (si == ti)
                    if same or ext not in AUDIO:
                        os.remove(sfp); dups_dropped += 1
                    else:
                        b, e = os.path.splitext(tfp); n = 2
                        while os.path.exists(f'{b} ({n}){e}'):
                            n += 1
                        shutil.move(sfp, f'{b} ({n}){e}'); kept_variants += 1
            shutil.rmtree(src, ignore_errors=True); folders_removed += 1

print(f"\n{'APLICADO' if args.apply else 'DRY-RUN'}: grupos a fusionar={merged_groups}"
      f" | nombres con albumes distintos dejados aparte={kept_separate}"
      + (f" | carpetas eliminadas={folders_removed} | archivos movidos={files_moved}"
         f" | dups descartados={dups_dropped} | variantes={kept_variants}" if args.apply else ""))
