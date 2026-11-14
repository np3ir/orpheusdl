"""One-time cleanup: merge EXISTING duplicate album folders left by past multi-service
downloads. The SAME album from Deezer/Tidal/Qobuz reports different years, so it landed
in separate folders:

    Artista/(2019) Album  +  Artista/(2020) Album   (mismo album, distinto servicio)

This merges them into ONE folder (the one with the MOST tracks is kept; ties -> earliest
year), moving tracks in and DE-DUPING by ISRC (same ISRC -> the better-quality copy stays, the
other goes to a reversible quarantine <dir>\_duplicados, never a hard delete; genuinely
different -> kept as " (N)"). Undo: python purge_non_flac.py --rollback config/merge_dupe_albums_journal.csv. Genuinely different same-named albums (different ISRCs, e.g. Chayanne
1988 vs 1998) are matched by ISRC and LEFT ALONE. Folders with no readable ISRC are left
alone (conservative).

    python merge_dupe_albums.py --dir "Z:\\"           # DRY-RUN (no cambia nada)
    python merge_dupe_albums.py --dir "Z:\\" --apply    # aplica
"""
import os, argparse, shutil
from collections import defaultdict

from purge_non_flac import quarantine_move
from utils.library_tools import isrc_of, quality_key

ap = argparse.ArgumentParser()
ap.add_argument('--dir', default='Z:\\')
ap.add_argument('--apply', action='store_true')
args = ap.parse_args()

AUDIO = {'.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav'}
ROOT = os.path.abspath(args.dir)
QDIR = os.path.join(ROOT, '_duplicados')
JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'merge_dupe_albums_journal.csv')


def strip_year(name):
    if len(name) > 7 and name[0] == '(' and name[5] == ')' and name[6] == ' ' and name[1:5].isdigit():
        return name[7:], name[1:5]
    return name, None


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
    _dirs[:] = [d for d in _dirs if d.lower() != '_duplicados']  # skip the quarantine
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
                    si = ti = None
                    if ext in AUDIO:
                        si, ti = isrc_of(sfp), isrc_of(tfp)
                    if ext not in AUDIO:
                        # cover/lyrics with the same name: the keeper's copy wins, ours is quarantined
                        quarantine_move([sfp], ROOT, QDIR, JOURNAL); dups_dropped += 1
                    elif si and ti and si == ti:
                        # same recording: keep the better quality, quarantine the other
                        if quality_key(sfp) > quality_key(tfp):
                            quarantine_move([tfp], ROOT, QDIR, JOURNAL)
                            shutil.move(sfp, tfp)
                        else:
                            quarantine_move([sfp], ROOT, QDIR, JOURNAL)
                        dups_dropped += 1
                    else:
                        # different recording (or no readable ISRC): keep both
                        b, e = os.path.splitext(tfp); n = 2
                        while os.path.exists(f'{b} ({n}){e}'):
                            n += 1
                        shutil.move(sfp, f'{b} ({n}){e}'); kept_variants += 1
            for r, _d, _f in os.walk(src, topdown=False):  # only empty folders: never rmtree files
                try:
                    os.rmdir(r)
                except OSError:
                    pass
            folders_removed += not os.path.exists(src)

print(f"\n{'APLICADO' if args.apply else 'DRY-RUN'}: grupos a fusionar={merged_groups}"
      f" | nombres con albumes distintos dejados aparte={kept_separate}"
      + (f" | carpetas eliminadas={folders_removed} | archivos movidos={files_moved}"
         f" | dups descartados={dups_dropped} | variantes={kept_variants}" if args.apply else ""))
