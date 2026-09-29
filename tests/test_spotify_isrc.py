"""Offline tests for utils/spotify_isrc.py (no network: Spotify is faked)."""
import json
from types import SimpleNamespace

import pytest
import requests

import utils.spotify_isrc as si
from utils.spotify_isrc import SpotifyIsrcLookup

QUOTA_BODY = {'error': {'status': 429, 'message': 'Too many requests', 'reason': 'QUOTA_EXCEEDED'}}


class FakeClient:
    """Stands in for the spotify module's WebApiClient (GET /v1/tracks/{id})."""

    token = 'tok'

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def track(self, tid):
        self.calls.append(tid)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeHttp:
    """Stands in for requests.get on GET /v1/playlists/{id}/items pages."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        page = self.pages.pop(0)
        if isinstance(page, requests.HTTPError):
            return page.response
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps(page).encode()
        return resp


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

    def init():  # like SpotifyAPI._init_web_api_client: a cleared client gets a new token
        if api.client is None:
            api.client = client
        return True
    api._init_web_api_client = init
    return SpotifyIsrcLookup(api, str(tmp_path), print_fn=(msgs.append if msgs is not None else lambda *a: None))


def track_row(tid, isrc, typ='track'):
    return {'is_local': False, 'item': {'id': tid, 'type': typ, 'external_ids': {'isrc': isrc} if isrc else {}}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(si.time, 'sleep', slept.append)
    return slept


# --------------------------------------------------------------- track lookups
def test_isrc_kept_for_the_run_only(tmp_path):
    old = tmp_path / 'spotify_isrc_cache.json'
    old.write_text(json.dumps({'spotify': {'t1': 'OLD000000001'}}))
    client = FakeClient([{'external_ids': {'isrc': 'usabc2400001'}}, {'external_ids': {'isrc': 'USABC2400001'}}])
    lk = lookup(tmp_path, client)
    assert not old.exists()                     # leftover persistent cache is removed
    assert lk.isrc('t1') == 'USABC2400001'
    assert lk.isrc('t1') == 'USABC2400001' and client.calls == ['t1']   # same run: asked once
    lk.save()
    assert not old.exists()
    lk2 = lookup(tmp_path, client)
    assert lk2.isrc('t1') == 'USABC2400001' and client.calls == ['t1', 't1']  # new run asks again


def test_429_stops_searching_and_reports_what_spotify_sent(tmp_path):
    client = FakeClient([{'external_ids': {'isrc': 'AAAAA0000001'}}, http_error(429, 16008, QUOTA_BODY)])
    msgs = []
    lk = lookup(tmp_path, client, msgs)
    assert lk.isrc('t1') == 'AAAAA0000001'
    assert lk.isrc('t2') is None and lk.isrc('t3') is None
    assert client.calls == ['t1', 't2']      # nothing asked after the 429
    assert len(msgs) == 1
    for part in ('429', 'QUOTA_EXCEEDED', 'Too many requests', 'Retry-After: 16008 s', '4h 26m'):
        assert part in msgs[0], part
    block = json.loads((tmp_path / 'spotify_blocked_until.json').read_text())['tracks']
    assert block['until'] > si.time.time() + 16000
    assert block['reason'] == 'QUOTA_EXCEEDED' and block['retry_after'] == 16008
    # a new run before Retry-After has passed makes no track call, and says why
    client2 = FakeClient([])
    msgs2 = []
    lk2 = lookup(tmp_path, client2, msgs2)
    assert lk2.isrc('t9') is None and client2.calls == [] and 'QUOTA_EXCEEDED' in msgs2[0]


def test_429_without_retry_after_backs_off_exponentially_then_stops(tmp_path, no_sleep):
    msgs = []
    client = FakeClient([http_error(429)] * 6)
    lk = lookup(tmp_path, client, msgs)
    assert lk.isrc('t1') is None and lk.stopped
    assert [s for s in no_sleep if s >= 1] == [1, 2, 4, 8, 16]
    assert len(client.calls) == 6
    assert 'no Retry-After sent' in msgs[0] and 'retry 1/5' in msgs[0]
    assert 'still limited after 5 retries' in msgs[-1]
    assert lk.isrc('t2') is None and len(client.calls) == 6      # stopped for the run
    assert not (tmp_path / 'spotify_blocked_until.json').exists()


def test_short_429_waits_retry_after_then_succeeds(tmp_path, no_sleep):
    client = FakeClient([http_error(429, 7, QUOTA_BODY), http_error(429, 1),
                         {'external_ids': {'isrc': 'GGGGG0000001'}}])
    msgs = []
    lk = lookup(tmp_path, client, msgs)
    assert lk.isrc('t1') == 'GGGGG0000001' and not lk.stopped
    # waits max(Retry-After, 1 s * 2^n): 7 (Retry-After) then 2 (backoff > Retry-After 1)
    assert [s for s in no_sleep if s >= 1] == [7, 2]
    assert 'QUOTA_EXCEEDED' in msgs[0] and 'Retry-After: 7 s' in msgs[0] and 'waiting 7 s' in msgs[0]


def test_block_expires(tmp_path):
    (tmp_path / 'spotify_blocked_until.json').write_text(json.dumps({'tracks': {'until': si.time.time() - 1}}))
    client = FakeClient([{'external_ids': {'isrc': 'BBBBB0000001'}}])
    lk = lookup(tmp_path, client)
    assert not lk.stopped and lk.isrc('t1') == 'BBBBB0000001'


def test_expired_token_401_is_renewed_once(tmp_path):
    client = FakeClient([http_error(401), {'external_ids': {'isrc': 'CCCCC0000001'}}])
    lk = lookup(tmp_path, client)
    assert lk.isrc('t1') == 'CCCCC0000001'
    assert client.calls == ['t1', 't1'] and not lk.stopped


def test_persistent_401_stops_with_spotify_message(tmp_path):
    body = {'error': {'status': 401, 'message': 'Invalid access token'}}
    msgs = []
    lk = lookup(tmp_path, FakeClient([http_error(401, body=body), http_error(401, body=body)]), msgs)
    assert lk.isrc('t1') is None and lk.stopped and lk.stopped_by['playlists']
    assert 'Invalid access token' in msgs[0]


def test_403_on_tracks_stops_everything(tmp_path):
    client = FakeClient([http_error(403)])
    lk = lookup(tmp_path, client)
    lk.isrc('t1')
    lk.isrc('t2')
    assert client.calls == ['t1'] and 'Premium' in lk.stopped and lk.stopped_by['playlists']


def test_5xx_is_retried_with_exponential_backoff(tmp_path, no_sleep):
    client = FakeClient([http_error(503), http_error(502), {'external_ids': {'isrc': 'EEEEE0000001'}}])
    lk = lookup(tmp_path, client)
    assert lk.isrc('t1') == 'EEEEE0000001'
    assert [s for s in no_sleep if s >= 1] == [1, 2]


def test_400_stops_the_bucket_with_the_message(tmp_path):
    body = {'error': {'status': 400, 'message': 'Invalid base62 id'}}
    msgs = []
    lk = lookup(tmp_path, FakeClient([http_error(400, body=body)]), msgs)
    assert lk.isrc('bad') is None and 'Invalid base62 id' in msgs[0] and lk.stopped


def test_missing_credentials_stops_without_calls(tmp_path):
    api = SimpleNamespace(client=None, _init_web_api_client=lambda: False)
    lk = SpotifyIsrcLookup(api, str(tmp_path), print_fn=lambda *a: None)
    assert lk.isrc('t1') is None and 'client_id' in lk.stopped


# ------------------------------------------------------------------ playlists
def test_playlist_read_50_per_call_using_item(tmp_path, monkeypatch):
    http = FakeHttp([
        {'items': [track_row('a', 'mxf140200162'), track_row('b', None), {'item': None},
                   track_row('ep', 'XXXXX0000000', typ='episode'),
                   {'track': {'id': 'd', 'type': 'track', 'external_ids': {'isrc': 'DEPREC000001'}}}],
         'next': 'https://api.spotify.com/v1/playlists/P/items?offset=50&limit=50'},
        {'items': [track_row('c', 'USAAA0000003'), {'is_local': True, 'item': {'id': None, 'type': 'track'}}],
         'next': None},
    ])
    monkeypatch.setattr(si.requests, 'get', http)
    client = FakeClient([])
    msgs = []
    lk = lookup(tmp_path, client, msgs)
    assert lk.prefetch_playlist('P') == 2
    assert http.urls == ['https://api.spotify.com/v1/playlists/P/items?limit=50',
                         'https://api.spotify.com/v1/playlists/P/items?offset=50&limit=50']
    assert '2 ISRC(s) read from the playlist in 2 call(s)' in msgs[0]
    assert lk.isrc('a') == 'MXF140200162' and lk.isrc('c') == 'USAAA0000003'
    assert client.calls == []
    assert not (tmp_path / 'spotify_isrc_cache.json').exists()


def test_playlist_bucket_works_while_tracks_are_blocked(tmp_path, monkeypatch):
    (tmp_path / 'spotify_blocked_until.json').write_text(
        json.dumps({'until': si.time.time() + 3600, 'status': 429, 'reason': 'QUOTA_EXCEEDED'}))  # 1st-version format
    monkeypatch.setattr(si.requests, 'get', FakeHttp([{'items': [track_row('a', 'AAAAA0000001')], 'next': None}]))
    client = FakeClient([])
    lk = lookup(tmp_path, client)
    assert lk.stopped and 'QUOTA_EXCEEDED' in lk.stopped
    assert lk.prefetch_playlist('P') == 1
    assert lk.isrc('a') == 'AAAAA0000001'
    assert lk.isrc('zz') is None and client.calls == []


def test_playlist_429_blocks_only_the_playlist_bucket(tmp_path, monkeypatch):
    http = FakeHttp([http_error(429, 16008, QUOTA_BODY)])
    monkeypatch.setattr(si.requests, 'get', http)
    client = FakeClient([{'external_ids': {'isrc': 'DDDDD0000001'}}])
    msgs = []
    lk = lookup(tmp_path, client, msgs)
    assert lk.prefetch_playlist('P') is None
    assert 'playlist reads' in msgs[0] and 'QUOTA_EXCEEDED' in msgs[0] and 'Retry-After: 16008 s' in msgs[0]
    assert lk.prefetch_playlist('P') is None and len(http.urls) == 1
    assert lk.isrc('t1') == 'DDDDD0000001'
    assert set(json.loads((tmp_path / 'spotify_blocked_until.json').read_text())) == {'playlists'}


def test_playlist_403_falls_back_to_track_lookups(tmp_path, monkeypatch):
    # Spec: get-playlists-items is owner/collaborator-only and answers 403 otherwise.
    body = {'error': {'status': 403, 'message': 'Forbidden'}}
    monkeypatch.setattr(si.requests, 'get', FakeHttp([http_error(403, body=body)]))
    client = FakeClient([{'external_ids': {'isrc': 'FFFFF0000001'}}])
    msgs = []
    lk = lookup(tmp_path, client, msgs)
    assert lk.prefetch_playlist('P') is None
    assert 'one by one' in msgs[0] and not lk.stopped
    assert lk.isrc('t1') == 'FFFFF0000001'
