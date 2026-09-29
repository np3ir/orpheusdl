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
    block = json.loads((tmp_path / 'spotify_blocked_until.json').read_text())
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
    (tmp_path / 'spotify_blocked_until.json').write_text(json.dumps({'until': si.time.time() - 1}))
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
