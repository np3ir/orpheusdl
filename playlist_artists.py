"""Extract the unique artists appearing in the user's playlists into per-service CSVs
(Nombre, URL, Apariciones), for qobuz / tidal / deezer. Reuses the playlist IDs in
mis_playlists.csv and each module's existing auth. Deezer uses the public API (no
account). A requests-per-minute limiter keeps authenticated services gentle.

    python playlist_artists.py --services tidal,deezer --limit 15 --suffix _muestra15
    python playlist_artists.py --services qobuz,tidal,deezer --rpm 45
"""
import os, csv, sys, time, argparse, json, requests

ap = argparse.ArgumentParser()
ap.add_argument('--services', default='qobuz,tidal,deezer')
ap.add_argument('--limit', type=int, default=0, help='max playlists per service (0 = all)')
ap.add_argument('--rpm', type=float, default=45.0, help='max API requests per minute (account-friendly pacing)')
ap.add_argument('--suffix', default='', help='appended to each output filename before .csv')
ap.add_argument('--outdir', default='.')
args = ap.parse_args()
services = [s.strip().lower() for s in args.services.split(',') if s.strip()]

_min_interval = 60.0 / args.rpm if args.rpm > 0 else 0.0
_last = [0.0]
def throttle():
    if _min_interval <= 0:
        return
    dt = time.monotonic() - _last[0]
    if dt < _min_interval:
        time.sleep(_min_interval - dt)
    _last[0] = time.monotonic()

# playlist ids per service from the CSV we already built
by_service = {'qobuz': [], 'tidal': [], 'deezer': []}
with open('mis_playlists.csv', encoding='utf-8-sig') as f:
    for r in csv.reader(f):
        if r and r[0].lower() in by_service:
            by_service[r[0].lower()].append(r[3].rstrip('/').split('/')[-1])

EX, OFF = 'modules/example', 'modules/_example_off'
moved = os.path.isdir(EX)
if moved:
    os.rename(EX, OFF)


def write_csv(service, artists, errors):
    out = os.path.join(args.outdir, f'artistas_{service}{args.suffix}.csv')
    rows = sorted(artists.items(), key=lambda kv: (-kv[1]['count'], kv[1]['name'].lower()))
    with open(out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['Nombre', 'URL', 'Apariciones'])
        for _, rec in rows:
            w.writerow([rec['name'], rec['url'], rec['count']])
    print(f'  -> {out}: {len(rows)} artistas unicos (errores: {errors})', flush=True)


def collect(artists, aid, name, url):
    if not aid or not name:
        return
    aid = str(aid)
    rec = artists.get(aid)
    if rec:
        rec['count'] += 1
    else:
        artists[aid] = {'name': name.strip(), 'url': url, 'count': 1}


try:
    from orpheus.core import Orpheus
    o = Orpheus()

    # ---------------- QOBUZ ----------------
    if 'qobuz' in services:
        ids = by_service['qobuz'][:args.limit] if args.limit else by_service['qobuz']
        print(f'QOBUZ: {len(ids)} playlists', flush=True)
        api = (o.load_module('qobuz') or o.loaded_modules.get('qobuz')).session
        artists, errors = {}, 0
        for n, pid in enumerate(ids, 1):
            try:
                offset, total = 0, None
                while True:
                    throttle()
                    data = api.get_playlist(pid, limit=500, offset=offset)
                    block = data.get('tracks') or {}
                    items = block.get('items') or []
                    total = block.get('total', len(items))
                    for t in items:
                        a = t.get('performer') or (t.get('album') or {}).get('artist') or {}
                        collect(artists, a.get('id'), a.get('name'), f"https://open.qobuz.com/artist/{a.get('id')}")
                    offset += len(items)
                    if not items or offset >= (total or 0):
                        break
            except Exception as e:
                errors += 1
                print(f'  ! qobuz {pid}: {repr(e)[:100]}', flush=True)
            if n % 25 == 0 or n == len(ids):
                print(f'  qobuz {n}/{len(ids)} | unicos: {len(artists)}', flush=True)
        write_csv('qobuz', artists, errors)

    # ---------------- TIDAL (public OpenAPI v2, client_credentials - NOT the account) ----------------
    if 'tidal' in services:
        ids = by_service['tidal'][:args.limit] if args.limit else by_service['tidal']
        print(f'TIDAL: {len(ids)} playlists (OpenAPI v2 publica, sin cuenta)', flush=True)
        tcfg = json.load(open('config/settings.json', encoding='utf-8'))['modules']['tidal']
        tok = {'v': None}

        def _tidal_auth():
            r = requests.post('https://auth.tidal.com/v1/oauth2/token',
                              data={'client_id': tcfg['guest_token'], 'client_secret': tcfg['guest_secret'],
                                    'grant_type': 'client_credentials'},
                              headers={'User-Agent': 'Mozilla/5.0', 'Origin': 'https://tidal.com'}, timeout=30)
            r.raise_for_status()
            tok['v'] = r.json()['access_token']

        _tidal_auth()
        artists, errors = {}, 0
        for n, uuid in enumerate(ids, 1):
            try:
                path = f'/playlists/{uuid}/relationships/items?countryCode=US&include=items.artists'
                refreshed = False
                pages, seen_paths = 0, set()
                MAX_PAGES = 250  # 250 x 20 = 5000 tracks; guards against a runaway cursor loop
                while path:
                    if path in seen_paths:
                        print(f'  ! tidal {uuid}: cursor repetido, corto paginacion', flush=True)
                        break
                    seen_paths.add(path)
                    pages += 1
                    if pages > MAX_PAGES:
                        print(f'  ! tidal {uuid}: >{MAX_PAGES} paginas, corto (playlist gigante o loop)', flush=True)
                        break
                    throttle()
                    resp = requests.get('https://openapi.tidal.com/v2' + path,
                                        headers={'Authorization': f'Bearer {tok["v"]}',
                                                 'accept': 'application/vnd.api+json'}, timeout=45)
                    if resp.status_code == 401 and not refreshed:
                        _tidal_auth(); refreshed = True
                        seen_paths.discard(path); pages -= 1  # retry same page after refresh
                        continue
                    resp.raise_for_status()
                    j = resp.json()
                    for x in j.get('included', []):
                        if x.get('type') == 'artists':
                            aid = x.get('id')
                            collect(artists, aid, (x.get('attributes') or {}).get('name'),
                                    f'https://tidal.com/artist/{aid}')
                    nxt = (j.get('links') or {}).get('next')
                    path = nxt or None
                    refreshed = False
            except Exception as e:
                errors += 1
                print(f'  ! tidal {uuid}: {repr(e)[:100]}', flush=True)
            if n % 25 == 0 or n == len(ids):
                print(f'  tidal {n}/{len(ids)} | unicos: {len(artists)}', flush=True)
        write_csv('tidal', artists, errors)

    # ---------------- DEEZER (public API, no account) ----------------
    if 'deezer' in services:
        ids = by_service['deezer'][:args.limit] if args.limit else by_service['deezer']
        print(f'DEEZER: {len(ids)} playlists (API publica)', flush=True)
        api = (o.load_module('deezer') or o.loaded_modules.get('deezer')).session
        artists, errors = {}, 0
        for n, pid in enumerate(ids, 1):
            try:
                throttle()
                tracks = api.get_playlist_tracks_public(pid, limit=100, max_tracks=100000)
                for t in (tracks or []):
                    a = t.get('artist') or {}
                    url = a.get('link') or f"https://www.deezer.com/artist/{a.get('id')}"
                    collect(artists, a.get('id'), a.get('name'), url)
            except Exception as e:
                errors += 1
                print(f'  ! deezer {pid}: {repr(e)[:100]}', flush=True)
            if n % 25 == 0 or n == len(ids):
                print(f'  deezer {n}/{len(ids)} | unicos: {len(artists)}', flush=True)
        write_csv('deezer', artists, errors)
finally:
    if moved and os.path.isdir(OFF):
        os.rename(OFF, EX)
print('DONE', flush=True)
