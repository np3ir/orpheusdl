"""Extract the unique artists appearing in the user's Qobuz playlists into a CSV
(Nombre, URL, Apariciones). Reuses the Qobuz IDs already listed in mis_playlists.csv
and the module's existing auth.

    python qobuz_artists.py --limit 20 --out artistas_qobuz_muestra20.csv
    python qobuz_artists.py --out artistas_qobuz.csv          # all playlists

A small pause between API calls keeps the load gentle on the account.
"""
import os, csv, sys, time, argparse

ap = argparse.ArgumentParser()
ap.add_argument('--limit', type=int, default=0, help='max playlists to process (0 = all)')
ap.add_argument('--out', default='artistas_qobuz.csv')
ap.add_argument('--pause', type=float, default=0.15, help='seconds between API calls')
ap.add_argument('--page', type=int, default=500, help='tracks per playlist page')
args = ap.parse_args()

# Qobuz playlist ids from the CSV we already built
ids = []
with open('mis_playlists.csv', encoding='utf-8-sig') as f:
    for r in csv.reader(f):
        if r and r[0] == 'Qobuz':
            ids.append(r[3].rstrip('/').split('/')[-1])
if args.limit:
    ids = ids[:args.limit]
print(f'Qobuz playlists a procesar: {len(ids)}', flush=True)

EX, OFF = 'modules/example', 'modules/_example_off'
moved = os.path.isdir(EX)
if moved:
    os.rename(EX, OFF)

artists = {}   # id -> {'name': str, 'count': int}
errors = 0
try:
    from orpheus.core import Orpheus
    o = Orpheus()
    api = (o.load_module('qobuz') or o.loaded_modules.get('qobuz')).session

    def add(entry):
        if not entry:
            return
        aid = entry.get('id')
        name = (entry.get('name') or '').strip()
        if not aid or not name:
            return
        rec = artists.get(aid)
        if rec:
            rec['count'] += 1
        else:
            artists[aid] = {'name': name, 'count': 1}

    for n, pid in enumerate(ids, 1):
        try:
            offset, total = 0, None
            while True:
                data = api.get_playlist(pid, limit=args.page, offset=offset)
                block = data.get('tracks') or {}
                items = block.get('items') or []
                total = block.get('total', len(items))
                for t in items:
                    # performer = the track's credited artist; fall back to album artist
                    add(t.get('performer') or (t.get('album') or {}).get('artist'))
                offset += len(items)
                time.sleep(args.pause)
                if not items or offset >= (total or 0):
                    break
        except Exception as e:
            errors += 1
            print(f'  ! playlist {pid} error: {repr(e)[:120]}', flush=True)
        if n % 10 == 0 or n == len(ids):
            print(f'  {n}/{len(ids)} playlists | artistas unicos: {len(artists)}', flush=True)
finally:
    if moved and os.path.isdir(OFF):
        os.rename(OFF, EX)

rows = sorted(artists.items(), key=lambda kv: (-kv[1]['count'], kv[1]['name'].lower()))
with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(['Nombre', 'URL', 'Apariciones'])
    for aid, rec in rows:
        w.writerow([rec['name'], f'https://open.qobuz.com/artist/{aid}', rec['count']])
print(f'LISTO: {len(rows)} artistas unicos -> {args.out} (errores: {errors})', flush=True)
