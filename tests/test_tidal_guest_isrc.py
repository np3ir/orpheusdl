"""Offline tests for abq's TIDAL guest-token (openapi v2) lookups (no network)."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

abq = pytest.importorskip('artist_best_quality')


def track(tid, isrc, tags=('LOSSLESS',), artist='7'):
    return {'id': tid, 'type': 'tracks',
            'attributes': {'isrc': isrc, 'mediaTags': list(tags), 'title': f't{tid}'},
            'relationships': {'artists': {'data': [{'id': artist, 'type': 'artists'}]}}}


class FakeGuest:
    """Replaces _tidal_guest_get: routes by path, records the paths requested."""

    def __init__(self, handler):
        self.handler = handler
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        return self.handler(path)


@pytest.fixture
def guest(monkeypatch):
    monkeypatch.setitem(abq._TIDAL_GUEST, 'tok', 'guest-token')
    monkeypatch.setattr(abq, '_TIDAL_ISRC_CACHE', {})
    monkeypatch.setattr(abq.time, 'sleep', lambda s: None)

    def install(handler):
        fake = FakeGuest(handler)
        monkeypatch.setattr(abq, '_tidal_guest_get', fake)
        return fake
    return install


def isrcs_of(path):
    return parse_qs(urlsplit(path).query)['filter[isrc]']


def no_account():
    def boom(isrc):
        raise AssertionError('account session must not be used')
    return SimpleNamespace(session=lambda name: SimpleNamespace(get_tracks_by_isrc=boom))


def test_prefetch_batches_20_per_request(guest):
    wanted = [f'USX{n:09d}' for n in range(45)]
    fake = guest(lambda p: {'data': [track(i, s) for i, s in enumerate(isrcs_of(p))], 'links': {}})
    abq._tidal_guest_isrc_fetch(wanted)
    assert [len(isrcs_of(p)) for p in fake.paths] == [20, 20, 5]
    assert all(p.startswith('/tracks?') and 'include=artists' in p for p in fake.paths)
    # later probes are served from the cache: no more requests, no account calls
    n = len(fake.paths)
    assert abq.probe_isrc(no_account(), 'tidal', wanted[0]) == ('0', 16, 44.1)
    assert len(fake.paths) == n


def test_probe_picks_best_quality_and_skips_lossy(guest):
    guest(lambda p: {'data': [track(1, 'AA', ('LOSSLESS',)), track(2, 'AA', ('HIRES_LOSSLESS', 'LOSSLESS')),
                              track(3, 'BB', ())]})
    abq._tidal_guest_isrc_fetch(['AA', 'BB', 'CC'])
    core = no_account()
    assert abq.probe_isrc(core, 'tidal', 'aa') == ('2', 24, 96.0)
    assert abq.probe_isrc(core, 'tidal', 'BB') is None   # lossy only
    assert abq.probe_isrc(core, 'tidal', 'CC') is None   # not on Tidal


def test_artist_id_from_guest_even_when_lossy(guest):
    guest(lambda p: {'data': [track(3, 'BB', (), artist='27')]})
    assert abq._artist_id_by_isrc(no_account(), 'tidal', 'BB') == '27'


def test_failed_guest_request_falls_back_to_account(guest):
    guest(lambda p: None)  # guest request failed after its retries
    calls = []

    def by_isrc(isrc):
        calls.append(isrc)
        return {'items': [{'id': 9, 'audioQuality': 'LOSSLESS'}]}
    core = SimpleNamespace(session=lambda name: SimpleNamespace(get_tracks_by_isrc=by_isrc))
    assert abq.probe_isrc(core, 'tidal', 'ZZ') == ('9', 16, 44.1)
    assert calls == ['ZZ']
    assert 'ZZ' not in abq._TIDAL_ISRC_CACHE  # failure is not cached as "absent"


def test_no_guest_credentials_uses_account(monkeypatch):
    monkeypatch.setitem(abq._TIDAL_GUEST, 'tok', None)
    monkeypatch.setattr(abq, '_TIDAL_ISRC_CACHE', {})
    monkeypatch.setattr(abq, '_tidal_guest_auth', lambda: None)
    core = SimpleNamespace(session=lambda name: SimpleNamespace(
        get_tracks_by_isrc=lambda isrc: {'items': [{'id': 5, 'audioQuality': 'HI_RES_LOSSLESS'}]}))
    assert abq.probe_isrc(core, 'tidal', 'QQ') == ('5', 24, 96.0)


def test_enumerate_requests_album_artists_and_own_albums_only_skips(guest, monkeypatch):
    monkeypatch.setattr(abq, '_tidal_guest_auth', lambda: 'guest-token')

    def handler(path):
        if path.startswith('/artists/'):
            return {'data': [{'id': 'A1', 'type': 'albums'}, {'id': 'A2', 'type': 'albums'}],
                    'included': [
                        {'id': 'A1', 'type': 'albums', 'attributes': {'releaseDate': '2020-01-01'},
                         'relationships': {'artists': {'data': [{'id': '7', 'type': 'artists'}]}}},
                        {'id': 'A2', 'type': 'albums', 'attributes': {'releaseDate': '2021-01-01'},
                         'relationships': {'artists': {'data': [{'id': '99', 'type': 'artists'}]}}},
                        {'id': '7', 'type': 'artists', 'attributes': {'name': 'Me'}}]}
        aid = path.split('/')[2]
        return {'included': [track(f'{aid}-1', f'ISRC{aid}')]}
    fake = guest(handler)
    out = abq.enumerate_tidal(None, '7', credited=False, own_albums_only=True, artist_name='Me')
    assert 'include=albums.artists' in fake.paths[0]
    assert set(out) == {'ISRCA1'}  # A2 (credited to another artist) skipped
