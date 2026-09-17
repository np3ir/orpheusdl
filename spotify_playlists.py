"""Add your Spotify playlists (owned + followed) to mis_playlists.csv, using the
saved user token in config/spotify/credentials.json. Run this when Spotify's rate
limit has cleared (it replaces any existing Spotify rows, so it's safe to re-run).
    python spotify_playlists.py
"""
import json, csv, time, os, requests

CID = '65b708073fc0480ea92a077233ca87bd'
CSV = 'mis_playlists.csv'
creds = json.load(open('config/spotify/credentials.json', encoding='utf-8'))
tok, ref = creds.get('access_token'), creds.get('refresh_token')
deadline = time.time() + 600


def refresh():
    global tok
    tr = requests.post('https://accounts.spotify.com/api/token',
                       data={'grant_type': 'refresh_token', 'refresh_token': ref, 'client_id': CID}, timeout=20).json()
    if tr.get('access_token'):
        tok = tr['access_token']
        creds['access_token'] = tok
        try:
            json.dump(creds, open('config/spotify/credentials.json', 'w'))
        except Exception:
            pass
        return True
    print('refresh:', str(tr)[:140], flush=True)
    return False


def sp_get(url):
    while time.time() < deadline:
        r = requests.get(url, headers={'Authorization': f'Bearer {tok}'}, timeout=25)
        if r.status_code == 401 and ref and refresh():
            continue
        if r.status_code == 429:
            w = min(int(r.headers.get('Retry-After', '10')) + 2, 65)
            print(f'429 wait {w}s', flush=True)
            time.sleep(w)
            continue
        return r
    return None


rows = []
url = 'https://api.spotify.com/v1/me/playlists?limit=50'
while url:
    r = sp_get(url)
    if r is None:
        print('Spotify still rate-limited - try again later.', flush=True)
        raise SystemExit(1)
    if r.status_code != 200:
        print('Spotify HTTP', r.status_code, r.text[:150], flush=True)
        raise SystemExit(1)
    d = r.json()
    for pl in d.get('items', []):
        if not pl:
            continue
        rows.append(['Spotify', pl.get('name'), (pl.get('tracks') or {}).get('total'),
                     (pl.get('external_urls') or {}).get('spotify') or f"https://open.spotify.com/playlist/{pl.get('id')}"])
    url = d.get('next')
print('Spotify playlists:', len(rows), flush=True)

# rewrite CSV: header + non-Spotify rows + fresh Spotify rows
existing = []
header = ['Servicio', 'Nombre', 'Tracks', 'URL']
if os.path.exists(CSV):
    with open(CSV, encoding='utf-8-sig') as f:
        rd = list(csv.reader(f))
    if rd:
        header = rd[0]
        existing = [x for x in rd[1:] if x and x[0] != 'Spotify']
with open(CSV, 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(header)
    for x in existing:
        w.writerow(x)
    for x in rows:
        w.writerow(x)
print('CSV updated:', len(existing) + len(rows), 'total rows', flush=True)
