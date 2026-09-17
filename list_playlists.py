"""List the user's playlists (owned + followed) across Tidal/Deezer/Qobuz/Spotify
into a CSV (service, name, tracks, url), using the OrpheusDL modules' existing auth."""
import os, csv, json
EX, OFF = 'modules/example', 'modules/_example_off'
moved = os.path.isdir(EX)
if moved:
    os.rename(EX, OFF)
rows = []  # (service, name, tracks, url)
import requests
try:
    from orpheus.core import Orpheus
    o = Orpheus()

    # ---------- TIDAL ----------
    try:
        api = (o.load_module('tidal') or o.loaded_modules.get('tidal')).session
        auth = api.authenticated_session()
        uid = getattr(auth, 'user_id', None) if auth else None
        if not uid:
            print('TIDAL: no user session', flush=True)
        else:
            n = 0
            for item in api.iter_user_playlist_entries(uid):
                pl = item.get('playlist') or item.get('item') or item
                uuid = pl.get('uuid')
                if not uuid:
                    continue
                rows.append(('Tidal', pl.get('title'), pl.get('numberOfTracks'),
                             f'https://tidal.com/playlist/{uuid}'))
                n += 1
            print('TIDAL playlists:', n, flush=True)
    except Exception as e:
        print('TIDAL error:', repr(e)[:200], flush=True)

    # ---------- QOBUZ (paged) ----------
    try:
        api = (o.load_module('qobuz') or o.loaded_modules.get('qobuz')).session
        offset, total, got = 0, None, 0
        while True:
            data = api.api_call('playlist/getUserPlaylists', params={'limit': 500, 'offset': offset})
            block = data.get('playlists') or {}
            items = block.get('items') or []
            total = block.get('total', len(items))
            for pl in items:
                rows.append(('Qobuz', pl.get('name'),
                             pl.get('tracks_count') or (pl.get('tracks') or {}).get('total'),
                             f"https://open.qobuz.com/playlist/{pl.get('id')}"))
            got += len(items)
            offset += len(items)
            if not items or offset >= (total or 0):
                break
        print('QOBUZ playlists:', got, flush=True)
    except Exception as e:
        print('QOBUZ error:', repr(e)[:200], flush=True)

    # ---------- DEEZER ----------
    try:
        api = (o.load_module('deezer') or o.loaded_modules.get('deezer')).session
        ud = api._api_call('deezer.getUserData')            # already the 'results' dict
        uid = (ud.get('USER') or {}).get('USER_ID')
        prof = api._api_call('deezer.pageProfile', {'user_id': uid, 'tab': 'playlists', 'nb': 2000})
        pls = ((prof.get('TAB') or {}).get('playlists') or {}).get('data') or []
        for pl in pls:
            rows.append(('Deezer', pl.get('TITLE'), pl.get('NB_SONG'),
                         f"https://www.deezer.com/playlist/{pl.get('PLAYLIST_ID')}"))
        print('DEEZER playlists:', len(pls), '(user', uid, ')', flush=True)
    except Exception as e:
        print('DEEZER error:', repr(e)[:200], flush=True)
finally:
    if moved and os.path.isdir(OFF):
        os.rename(OFF, EX)

# ---------- SPOTIFY (stored user token, refresh with the module's OAuth app id) ----------
try:
    OAUTH_CID = '65b708073fc0480ea92a077233ca87bd'  # OrpheusDL Spotify module OAuth client (PKCE, public)
    creds = json.load(open('config/spotify/credentials.json', encoding='utf-8'))
    tok, ref = creds.get('access_token'), creds.get('refresh_token')

    def sp_get(url, token):
        return requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=20)

    if sp_get('https://api.spotify.com/v1/me', tok).status_code == 401 and ref:
        tr = requests.post('https://accounts.spotify.com/api/token',
                           data={'grant_type': 'refresh_token', 'refresh_token': ref, 'client_id': OAUTH_CID},
                           timeout=20).json()
        if tr.get('access_token'):
            tok = tr['access_token']
        else:
            print('SPOTIFY refresh failed:', str(tr)[:160], flush=True)
    url, n = 'https://api.spotify.com/v1/me/playlists?limit=50', 0
    while url:
        r = sp_get(url, tok)
        if r.status_code != 200:
            print('SPOTIFY http', r.status_code, r.text[:160], flush=True)
            break
        d = r.json()
        for pl in d.get('items', []):
            if not pl:
                continue
            rows.append(('Spotify', pl.get('name'), (pl.get('tracks') or {}).get('total'),
                         (pl.get('external_urls') or {}).get('spotify') or f"https://open.spotify.com/playlist/{pl.get('id')}"))
            n += 1
        url = d.get('next')
    print('SPOTIFY playlists:', n, flush=True)
except Exception as e:
    print('SPOTIFY error:', repr(e)[:200], flush=True)

out = r'D:/OrpheusDL/mis_playlists.csv'
with open(out, 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(['Servicio', 'Nombre', 'Tracks', 'URL'])
    for row in rows:
        w.writerow(row)
print('TOTAL rows:', len(rows), '->', out, flush=True)
