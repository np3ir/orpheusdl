"""Offline tests for utils/spotify_isrc.py (no network: the Spotify client is faked)."""
import json
from types import SimpleNamespace

import pytest
import requests

import utils.spotify_isrc as si
from utils.spotify_isrc import SpotifyIsrcLookup


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def track(self, tid):
        self.calls.append(tid)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


QUOTA_BODY = {'error': {'status': 429, 'message': 'Too many requests', 'reason': 'QUOTA_EXCEEDED'}}


def http_error(code, retry_after=None, body=None):
    resp = requests.Response()
    resp.status_code = code
    if retry_after is not None:
        resp.headers['Retry-After'] = str(retry_after)
    if body is not None:
        resp._content = json.dumps(body).encode()
    return requests.HTTPError(response=resp)


def lookup(tmp_path, client, msgs=None):
    api = SimpleNamespace(client=client)

    def init():  # a cleared client gets a "new token" (same fake client)
        if api.client is None:
            api.client = client
        return True
    api._init_web_api_client = init
    return SpotifyIsrcLookup(api, str(tmp_path), print_fn=(msgs.append if msgs is not None else lambda *a: None))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(si.time, 'sleep', lambda s: None)


def test_hit_is_cached_across_runs(tmp_path):
    client = FakeClient([{'external_ids': {'isrc': 'usabc2400001'}}])
    lk = lookup(tmp_path, client)
    assert lk.isrc('t1') == 'USABC2400001'
    lk.save()
    lk2 = lookup(tmp_path, FakeClient([]))
    assert lk2.isrc('t1') == 'USABC2400001'
    assert client.calls == ['t1'] and lk2.stats['cache'] == 1


def test_429_stops_searching_and_persists_block(tmp_path):
    client = FakeClient([{'external_ids': {'isrc': 'AAAAA0000001'}}, http_error(429, 16008, QUOTA_BODY)])
    msgs = []
    lk = lookup(tmp_path, client, msgs)
    assert lk.isrc('t1') == 'AAAAA0000001'
    assert lk.isrc('t2') is None
    assert lk.isrc('t3') is None
    assert lk.isrc('t1') == 'AAAAA0000001'   # cached ISRCs still returned after the stop
    assert client.calls == ['t1', 't2']      # nothing asked after the 429
    assert len(msgs) == 1
    # the message carries what Spotify sent back
    for part in ('429', 'QUOTA_EXCEEDED', 'Too many requests', 'Retry-After: 16008 s', '4h 26m'):
        assert part in msgs[0], part
    block = json.loads((tmp_path / 'spotify_blocked_until.json').read_text())['tracks']
    assert block['until'] > si.time.time() + 16000
    assert block['reason'] == 'QUOTA_EXCEEDED' and block['retry_after'] == 16008
    lk.save()
    # a new run while still blocked makes no Spotify call at all, and says why
    client2 = FakeClient([])
    msgs2 = []
    lk2 = lookup(tmp_path, client2, msgs2)
    assert lk2.isrc('t9') is None and lk2.isrc('t1') == 'AAAAA0000001'
    assert client2.calls == [] and 'QUOTA_EXCEEDED' in msgs2[0]


def test_429_without_retry_after_or_body(tmp_path):
    msgs = []
    lk = lookup(tmp_path, FakeClient([http_error(429)]), msgs)
    assert lk.isrc('t1') is None and lk.stopped
    assert 'no Retry-After sent' in msgs[0]
    assert not (tmp_path / 'spotify_blocked_until.json').exists()


def test_short_429_also_stops(tmp_path):
    client = FakeClient([http_error(429, 2)])
    lk = lookup(tmp_path, client)
    assert lk.isrc('t1') is None and lk.isrc('t2') is None
    assert client.calls == ['t1'] and lk.stopped


def test_block_expires(tmp_path):
    (tmp_path / 'spotify_blocked_until.json').write_text(json.dumps({'tracks': {'until': si.time.time() - 1}}))
    client = FakeClient([{'external_ids': {'isrc': 'BBBBB0000001'}}])
    lk = lookup(tmp_path, client)
    assert not lk.stopped and lk.isrc('t1') == 'BBBBB0000001'


def test_403_stops(tmp_path):
    client = FakeClient([http_error(403)])
    lk = lookup(tmp_path, client)
    lk.isrc('t1')
    lk.isrc('t2')
    assert client.calls == ['t1'] and 'Premium' in lk.stopped


def test_expired_token_401_is_renewed_once(tmp_path):
    first = FakeClient([http_error(401)])
    second = FakeClient([{'external_ids': {'isrc': 'CCCCC0000001'}}])
    fresh = iter([second])
    api = SimpleNamespace(client=first)

    def init():
        if api.client is None:  # like SpotifyAPI._init_web_api_client: new token only when cleared
            api.client = next(fresh)
        return True
    api._init_web_api_client = init
    lk = SpotifyIsrcLookup(api, str(tmp_path), print_fn=lambda *a: None)
    assert lk.isrc('t1') == 'CCCCC0000001'
    assert first.calls == ['t1'] and second.calls == ['t1'] and not lk.stopped


def test_persistent_401_stops_with_spotify_message(tmp_path):
    body = {'error': {'status': 401, 'message': 'Invalid access token'}}
    msgs = []
    lk = lookup(tmp_path, FakeClient([http_error(401, body=body), http_error(401, body=body)]), msgs)
    assert lk.isrc('t1') is None and lk.stopped
    assert 'Invalid access token' in msgs[0]


def test_missing_credentials_stops_without_calls(tmp_path):
    api = SimpleNamespace(client=None, _init_web_api_client=lambda: False)
    lk = SpotifyIsrcLookup(api, str(tmp_path), print_fn=lambda *a: None)
    assert lk.isrc('t1') is None and 'client_id' in lk.stopped


# ------------------------------------------------------------------ playlists
class FakeHttp:
    """Stands in for requests.get on GET /playlists/{id}/items pages."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        page = self.pages.pop(0)
        resp = requests.Response()
        if isinstance(page, requests.HTTPError):
            resp = page.response
        else:
            resp.status_code = 200
            resp._content = json.dumps(page).encode()
        return resp


def track_row(tid, isrc, key='item'):
    return {key: {'id': tid, 'type': 'track', 'external_ids': {'isrc': isrc} if isrc else {}}}


def test_playlist_is_read_100_per_call_into_the_cache(tmp_path, monkeypatch):
    http = FakeHttp([
        {'items': [track_row('a', 'mxf140200162'), track_row('b', None), {'item': None}],
         'next': 'https://api.spotify.com/v1/playlists/P/items?offset=100&limit=100'},
        {'items': [track_row('c', 'USAAA0000003', key='track'), {'track': {'id': None, 'is_local': True}}],
         'next': None},
    ])
    monkeypatch.setattr(si.requests, 'get', http)
    client = FakeClient([])          # per-track endpoint must not be needed for a/c
    client.token = 'tok'
    msgs = []
    lk = lookup(tmp_path, client, msgs)
    assert lk.prefetch_playlist('P') == 2
    assert http.urls[0] == 'https://api.spotify.com/v1/playlists/P/items?limit=100'
    assert len(http.urls) == 2 and '2 ISRC(s) read from the playlist in 2 call(s)' in msgs[0]
    assert lk.isrc('a') == 'MXF140200162' and lk.isrc('c') == 'USAAA0000003'
    assert client.calls == [] and lk.stats['spotify'] == 2
    saved = json.loads((tmp_path / 'spotify_isrc_cache.json').read_text())['spotify']
    assert saved == {'a': 'MXF140200162', 'c': 'USAAA0000003'}


def test_playlist_bucket_works_while_tracks_are_blocked(tmp_path, monkeypatch):
    (tmp_path / 'spotify_blocked_until.json').write_text(
        json.dumps({'until': si.time.time() + 3600, 'status': 429, 'reason': 'QUOTA_EXCEEDED'}))  # old format
    monkeypatch.setattr(si.requests, 'get', FakeHttp([{'items': [track_row('a', 'AAAAA0000001')], 'next': None}]))
    client = FakeClient([])
    client.token = 'tok'
    lk = lookup(tmp_path, client)
    assert lk.stopped and 'QUOTA_EXCEEDED' in lk.stopped
    assert lk.prefetch_playlist('P') == 1
    assert lk.isrc('a') == 'AAAAA0000001'
    assert lk.isrc('zz') is None and client.calls == []


def test_playlist_429_blocks_only_the_playlist_bucket(tmp_path, monkeypatch):
    http = FakeHttp([http_error(429, 600, QUOTA_BODY)])
    monkeypatch.setattr(si.requests, 'get', http)
    client = FakeClient([{'external_ids': {'isrc': 'DDDDD0000001'}}])
    client.token = 'tok'
    msgs = []
    lk = lookup(tmp_path, client, msgs)
    assert lk.prefetch_playlist('P') is None
    assert 'playlist reads' in msgs[0] and 'QUOTA_EXCEEDED' in msgs[0] and 'Retry-After: 600 s' in msgs[0]
    assert lk.prefetch_playlist('P') is None and len(http.urls) == 1   # no second playlist call
    assert lk.isrc('t1') == 'DDDDD0000001'                             # track lookups unaffected
    blocks = json.loads((tmp_path / 'spotify_blocked_until.json').read_text())
    assert set(blocks) == {'playlists'}
