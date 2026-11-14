"""Move " (2)"/" (3)"... numbered duplicate audio files that are the SAME
recording (same ISRC) as the un-numbered original in the same folder to a reversible
quarantine folder (<dir>\\_duplicados), never a hard delete.

    python dedup_numbered.py --dir "Z:\\"          # DRY-RUN (lista, no mueve)
    python dedup_numbered.py --dir "Z:\\" --apply  # mueve los duplicados a cuarentena

Only moves a "<name> (N).ext" when "<name>.ext" exists in the same folder, both carry the
same non-empty ISRC AND the numbered copy is not better quality than the original.
A better-quality numbered copy is kept and reported. Files without a readable ISRC are
left alone. Undo: python purge_non_flac.py --rollback config/dedup_numbered_journal.csv
"""
import os, re, argparse

from purge_non_flac import quarantine_move
from utils.library_tools import isrc_of, quality_key

ap = argparse.ArgumentParser()
ap.add_argument('--dir', default='Z:\\')
ap.add_argument('--apply', action='store_true')
args = ap.parse_args()

AUDIO_EXT = {'.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav'}
NUM_RE = re.compile(r'^(?P<base>.+) \(\d+\)(?P<ext>\.[^.]+)$')
ROOT = os.path.abspath(args.dir)
QDIR = os.path.join(ROOT, '_duplicados')
JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'dedup_numbered_journal.csv')

count = removed = freed = kept_better = 0
for dp, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d.lower() != '_duplicados']  # never re-scan the quarantine
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
            continue  # only touch confirmed same-recording duplicates
        if quality_key(numbered) > quality_key(base):
            kept_better += 1
            print("KEEP (mejor calidad que el original) " + numbered)
            continue
        sz = os.path.getsize(numbered)
        count += 1
        freed += sz
        print(("MOVE " if args.apply else "DRY ") + numbered)
        if args.apply:
            victims = [numbered]
            lrc = os.path.splitext(numbered)[0] + '.lrc'  # sidecar lyrics
            if os.path.isfile(lrc):
                victims.append(lrc)
            if quarantine_move(victims, ROOT, QDIR, JOURNAL):
                removed += 1

print(f"\n{'MOVIDOS' if args.apply else 'CANDIDATOS'}: {removed if args.apply else count}"
      f" | conservados por mejor calidad: {kept_better}"
      f" | espacio {'en cuarentena' if args.apply else 'recuperable'}: {freed/1024/1024:.1f} MB")
if args.apply and removed:
    print(f"Cuarentena: {QDIR}\nPara deshacer: python purge_non_flac.py --rollback \"{JOURNAL}\"")
