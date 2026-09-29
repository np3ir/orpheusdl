"""Offline tests for utils/spotify_isrc.py (no network: Spotify is faked)."""
import base64
import json
from types import SimpleNamespace

import pytest
import requests

import utils.spotify_isrc as si
from utils.spotify_isrc import SpotifyIsrcLookup

QUOTA_BODY = {'error': {'status': 429, 'message': 'Too many requests', 'reason': 'QUOTA_EXCEEDED'}}


def response(code=200, body=None, retry_after=None):
    resp = requests.Response()
    resp.status_code = code
    if retry_after is not None:
        resp.headers['Retry-After'] = str(retry_after)
    if body is not None:
        resp._content = json.dumps(body).encode()
    return resp


def err(code, retry_after=None, body=None):
    return response(code, body, retry_after)


def track(isrc):
    return {'id': 'x', 'type': 'track', 'external_ids': {'isrc': isrc} if isrc else {}}


class FakeSpotify:
    """Fakes requests.post (token endpoint) and requests.get (Web API).

    get_responses: queue of dicts (200 body) or Responses (errors), consumed in order."""

    def __init__(self, get_responses=(), token_responses=None, expires_in=3600):
        self.gets = list(get_responses)
        self.tokens = list(token_responses or [])
        self.expires_in = expires_in
        self.urls = []
        self.token_calls = []
        self.issued = 0

    def post(self, url, data=None, auth=None, timeout=None, headers=None):
        self.token_calls.append({'url': url, 'data': data, 'auth': auth})
        if self.tokens:
            return self.tokens.pop(0)
        self.issued += 1
        return response(200, {'access_token': f'tok{self.issued}', 'token_type': 'Bearer',
                              'expires_in': self.expires_in})

    def get(self, url, headers=None, timeout=None):
        self.urls.append((url, headers['Authorization']))
        r = self.gets.pop(0)
        return r if isinstance(r, requests.Response) else response(200, r)

    @property
    def track_calls(self):
        return [u.rsplit('/', 1)[-1] for u, _ in self.urls if '/v1/tracks/' in u]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(si.time, 'sleep', slept.append)
    return slept


@pytest.fixture
def spotify(monkeypatch):
    def install(*a, **k):
        fake = FakeSpotify(*a, **k)
        monkeypatch.setattr(si.requests, 'post', fake.post)
        monkeypatch.setattr(si.requests, 'get', fake.get)
        return fake
    return install


def lookup(tmp_path, msgs=None, cfg=None):
    api = SimpleNamespace(config=cfg if cfg is not None else {'client_id': 'cid', 'client_secret': 'sec'})
    return SpotifyIsrcLookup(api, str(tmp_path), print_fn=(msgs.append if msgs is not None else lambda *a: None))


def row(tid, isrc, typ='track'):
    return {'is_local': False, 'item': {'id': tid, 'type': typ, 'external_ids': {'isrc': isrc} if isrc else {}}}


# ------------------------------------------------------------------- token
def test_token_request_follows_the_client_credentials_tutorial(tmp_path, spotify):
    fake = spotify([track('usabc2400001')])
    lk = lookup(tmp_path)
    assert lk.isrc('t1') == 'USABC2400001'
    call = fake.token_calls[0]
    assert call['url'] == 'https://accounts.spotify.com/api/token'
    assert call['data'] == {'grant_type': 'client_credentials'}      # secret NOT in the body
    assert call['auth'] == ('cid', 'sec')                            # requests -> Authorization: Basic
    assert requests.auth._basic_auth_str('cid', 'sec') == 'Basic ' + base64.b64encode(b'cid:sec').decode()
    assert fake.urls == [('https://api.spotify.com/v1/tracks/t1', 'Bearer tok1')]


def test_token_is_reused_then_renewed_before_expires_in(tmp_path, spotify, monkeypatch):
    fake = spotify([track('A0000000001'), track('A0000000002'), track('A0000000003')])
    clock = [1000.0]
    monkeypatch.setattr(si.time, 'monotonic', lambda: clock[0])
    lk = lookup(tmp_path)
    lk.isrc('t1')
    clock[0] += 3000                     # still > 60 s before the 3600 s expiry
    lk.isrc('t2')
    clock[0] += 600                      # now inside the renewal margin
    lk.isrc('t3')
    assert len(fake.token_calls) == 2
    assert [a for _, a in fake.urls] == ['Bearer tok1', 'Bearer tok1', 'Bearer tok2']


def test_rejected_credentials_report_oauth_error(tmp_path, spotify):
    fake = spotify(token_responses=[response(400, {'error': 'invalid_client',
                                                   'error_description': 'Invalid client secret'})])
    msgs = []
    lk = lookup(tmp_path, msgs)
    assert lk.isrc('t1') is None and lk.stopped and lk.stopped_by['playlists']
    assert 'invalid_client / Invalid client secret' in msgs[0]
    assert fake.urls == []


def test_missing_credentials_stops_without_calls(tmp_path, spotify):
    fake = spotify()
    lk = lookup(tmp_path, cfg={})
    assert lk.isrc('t1') is None and 'client_id' in lk.stopped
    assert fake.token_calls == [] and fake.urls == []


def test_expired_token_401_gets_a_new_token_once(tmp_path, spotify):
    fake = spotify([err(401), track('CCCCC0000001')])
    lk = lookup(tmp_path)
    assert lk.isrc('t1') == 'CCCCC0000001' and not lk.stopped
    assert [a for _, a in fake.urls] == ['Bearer tok1', 'Bearer tok2']


def test_persistent_401_stops_with_spotify_message(tmp_path, spotify):
    body = {'error': {'status': 401, 'message': 'Invalid access token'}}
    spotify([err(401, body=body), err(401, body=body)])
    msgs = []
    lk = lookup(tmp_path, msgs)
    assert lk.isrc('t1') is None and lk.stopped and lk.stopped_by['playlists']
    assert 'Invalid access token' in msgs[0]


# ------------------------------------------------------------- track lookups
def test_isrc_kept_for_the_run_only(tmp_path, spotify):
    old = tmp_path / 'spotify_isrc_cache.json'
    old.write_text(json.dumps({'spotify': {'t1': 'OLD000000001'}}))
    fake = spotify([track('usabc2400001'), track('USABC2400001')])
    lk = lookup(tmp_path)
    assert not old.exists()                     # leftover persistent cache is removed
    assert lk.isrc('t1') == 'USABC2400001'
    assert lk.isrc('t1') == 'USABC2400001' and fake.track_calls == ['t1']   # same run: asked once
    lk.save()
    assert not old.exists()
    lk2 = lookup(tmp_path)
    assert lk2.isrc('t1') == 'USABC2400001' and fake.track_calls == ['t1', 't1']  # new run asks again


def test_long_429_stops_and_reports_what_spotify_sent(tmp_path, spotify):
    fake = spotify([track('AAAAA0000001'), err(429, 16008, QUOTA_BODY)])
    msgs = []
    lk = lookup(tmp_path, msgs)
    assert lk.isrc('t1') == 'AAAAA0000001'
    assert lk.isrc('t2') is None and lk.isrc('t3') is None
    assert fake.track_calls == ['t1', 't2']      # nothing asked after the 429
    assert len(msgs) == 1
    for part in ('429', 'QUOTA_EXCEEDED', 'Too many requests', 'Retry-After: 16008 s', '4h 26m'):
        assert part in msgs[0], part
    block = json.loads((tmp_path / 'spotify_blocked_until.json').read_text())['tracks']
    assert block['until'] > si.time.time() + 16000
    assert block['reason'] == 'QUOTA_EXCEEDED' and block['retry_after'] == 16008
    # a new run before Retry-After has passed makes no track call, and says why
    msgs2 = []
    lk2 = lookup(tmp_path, msgs2)
    assert lk2.isrc('t9') is None and fake.track_calls == ['t1', 't2'] and 'QUOTA_EXCEEDED' in msgs2[0]


def test_429_without_retry_after_backs_off_exponentially_then_stops(tmp_path, spotify, no_sleep):
    fake = spotify([err(429)] * 6)
    msgs = []
    lk = lookup(tmp_path, msgs)
    assert lk.isrc('t1') is None and lk.stopped
    assert [s for s in no_sleep if s >= 1] == [1, 2, 4, 8, 16]
    assert len(fake.track_calls) == 6
    assert 'no Retry-After sent' in msgs[0] and 'retry 1/5' in msgs[0]
    assert 'still limited after 5 retries' in msgs[-1]
    assert lk.isrc('t2') is None and len(fake.track_calls) == 6      # stopped for the run
    assert not (tmp_path / 'spotify_blocked_until.json').exists()


def test_short_429_waits_retry_after_then_succeeds(tmp_path, spotify, no_sleep):
    spotify([err(429, 7, QUOTA_BODY), err(429, 1), track('GGGGG0000001')])
    msgs = []
    lk = lookup(tmp_path, msgs)
    assert lk.isrc('t1') == 'GGGGG0000001' and not lk.stopped
    # waits max(Retry-After, 1 s * 2^n): 7 (Retry-After) then 2 (backoff > Retry-After 1)
    assert [s for s in no_sleep if s >= 1] == [7, 2]
    assert 'QUOTA_EXCEEDED' in msgs[0] and 'Retry-After: 7 s' in msgs[0] and 'waiting 7 s' in msgs[0]


def test_block_expires(tmp_path, spotify):
    (tmp_path / 'spotify_blocked_until.json').write_text(json.dumps({'tracks': {'until': si.time.time() - 1}}))
    spotify([track('BBBBB0000001')])
    lk = lookup(tmp_path)
    assert not lk.stopped and lk.isrc('t1') == 'BBBBB0000001'


def test_403_on_tracks_stops_everything(tmp_path, spotify):
    fake = spotify([err(403)])
    lk = lookup(tmp_path)
    lk.isrc('t1')
    lk.isrc('t2')
    assert fake.track_calls == ['t1'] and 'Premium' in lk.stopped and lk.stopped_by['playlists']


def test_5xx_is_retried_with_exponential_backoff(tmp_path, spotify, no_sleep):
    spotify([err(503), err(502), track('EEEEE0000001')])
    lk = lookup(tmp_path)
    assert lk.isrc('t1') == 'EEEEE0000001'
    assert [s for s in no_sleep if s >= 1] == [1, 2]


def test_400_stops_the_bucket_with_the_message(tmp_path, spotify):
    spotify([err(400, body={'error': {'status': 400, 'message': 'Invalid base62 id'}})])
    msgs = []
    lk = lookup(tmp_path, msgs)
    assert lk.isrc('bad') is None and 'Invalid base62 id' in msgs[0] and lk.stopped


# ------------------------------------------------------------------ playlists
def test_playlist_read_50_per_call_using_item(tmp_path, spotify):
    fake = spotify([
        {'items': [row('a', 'mxf140200162'), row('b', None), {'item': None},
                   row('ep', 'XXXXX0000000', typ='episode'),
                   {'track': {'id': 'd', 'type': 'track', 'external_ids': {'isrc': 'DEPREC000001'}}}],
         'next': 'https://api.spotify.com/v1/playlists/P/items?offset=50&limit=50'},
        {'items': [row('c', 'USAAA0000003'), {'is_local': True, 'item': {'id': None, 'type': 'track'}}],
         'next': None},
    ])
    msgs = []
    lk = lookup(tmp_path, msgs)
    assert lk.prefetch_playlist('P') == 2
    assert [u for u, _ in fake.urls] == ['https://api.spotify.com/v1/playlists/P/items?limit=50',
                                         'https://api.spotify.com/v1/playlists/P/items?offset=50&limit=50']
    assert '2 ISRC(s) read from the playlist in 2 call(s)' in msgs[0]
    assert lk.isrc('a') == 'MXF140200162' and lk.isrc('c') == 'USAAA0000003'
    assert fake.track_calls == []
    assert not (tmp_path / 'spotify_isrc_cache.json').exists()


def test_playlist_bucket_works_while_tracks_are_blocked(tmp_path, spotify):
    (tmp_path / 'spotify_blocked_until.json').write_text(
        json.dumps({'until': si.time.time() + 3600, 'status': 429, 'reason': 'QUOTA_EXCEEDED'}))  # 1st-version format
    fake = spotify([{'items': [row('a', 'AAAAA0000001')], 'next': None}])
    lk = lookup(tmp_path)
    assert lk.stopped and 'QUOTA_EXCEEDED' in lk.stopped
    assert lk.prefetch_playlist('P') == 1
    assert lk.isrc('a') == 'AAAAA0000001'
    assert lk.isrc('zz') is None and fake.track_calls == []


def test_playlist_429_blocks_only_the_playlist_bucket(tmp_path, spotify):
    fake = spotify([err(429, 16008, QUOTA_BODY), track('DDDDD0000001')])
    msgs = []
    lk = lookup(tmp_path, msgs)
    assert lk.prefetch_playlist('P') is None
    assert 'playlist reads' in msgs[0] and 'QUOTA_EXCEEDED' in msgs[0] and 'Retry-After: 16008 s' in msgs[0]
    assert lk.prefetch_playlist('P') is None and len(fake.urls) == 1
    assert lk.isrc('t1') == 'DDDDD0000001'
    assert set(json.loads((tmp_path / 'spotify_blocked_until.json').read_text())) == {'playlists'}


def test_playlist_403_falls_back_to_track_lookups(tmp_path, spotify):
    # Spec: get-playlists-items is owner/collaborator-only and answers 403 otherwise.
    spotify([err(403, body={'error': {'status': 403, 'message': 'Forbidden'}}), track('FFFFF0000001')])
    msgs = []
    lk = lookup(tmp_path, msgs)
    assert lk.prefetch_playlist('P') is None
    assert 'one by one' in msgs[0] and not lk.stopped
    assert lk.isrc('t1') == 'FFFFF0000001'
