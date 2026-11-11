"""Offline tests for Deezer share-link resolution (no network)."""
import pytest
import requests

from utils import short_links as sl

REAL = 'https://www.deezer.com/track/4371720031'
DEST = ('https://link.deezer.com/?awf=https%3A%2F%2Fwww.deezer.com%2Ftrack%2F4371720031%3Fhost%3D1%26utm_source%3Dx'
        '&dest=https%3A%2F%2Fwww.deezer.com%2Ftrack%2F4371720031%3Fhost%3D1%26utm_source%3Dx')


class R:
    def __init__(self, status=301, location=None):
        self.status_code = status
        self.headers = {'Location': location} if location else {}


def fake(*responses):
    queue = list(responses)
    calls = []

    def get(url, **kw):
        calls.append((url, kw))
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return get, calls


def test_is_short_link():
    assert sl.is_short_link('https://link.deezer.com/s/abc')
    assert sl.is_short_link('link.deezer.com/s/abc')
    assert sl.is_short_link('https://deezer.page.link/xyz')
    assert not sl.is_short_link('https://www.deezer.com/track/1')
    assert not sl.is_short_link('https://tidal.com/album/1')


def test_regular_urls_pass_through_without_network():
    get, calls = fake()
    assert sl.resolve('https://www.deezer.com/track/1', get=get) == 'https://www.deezer.com/track/1'
    assert calls == []


def test_dest_parameter_gives_clean_media_url():
    get, calls = fake(R(301, DEST))
    assert sl.resolve('https://link.deezer.com/s/abc', get=get) == REAL
    assert calls[0][1]['allow_redirects'] is False


def test_direct_redirect_to_deezer_media_url():
    get, _ = fake(R(302, 'https://www.deezer.com/en/playlist/123?utm_source=x'))
    assert sl.resolve('https://link.deezer.com/s/abc', get=get) == 'https://www.deezer.com/en/playlist/123'


def test_follows_a_hop_between_short_hosts():
    get, calls = fake(R(302, 'https://link.deezer.com/s2/def'), R(301, DEST))
    assert sl.resolve('https://link.deezer.com/s/abc', get=get) == REAL
    assert len(calls) == 2


def test_never_follows_to_another_host():
    get, calls = fake(R(302, 'https://evil.example/track/1'))
    with pytest.raises(sl.ShortLinkError):
        sl.resolve('https://link.deezer.com/s/abc', get=get)
    assert len(calls) == 1


def test_dest_pointing_outside_deezer_is_ignored():
    evil = 'https://link.deezer.com/?dest=https%3A%2F%2Fevil.example%2Ftrack%2F1'
    get, _ = fake(R(301, evil), R(301, evil), R(301, evil), R(301, evil), R(301, evil))
    with pytest.raises(sl.ShortLinkError):
        sl.resolve('https://link.deezer.com/s/abc', get=get)


def test_no_redirect_is_an_error():
    get, _ = fake(R(404))
    with pytest.raises(sl.ShortLinkError):
        sl.resolve('https://link.deezer.com/s/abc', get=get)


def test_network_error_is_wrapped():
    get, _ = fake(requests.ConnectionError('boom'))
    with pytest.raises(sl.ShortLinkError):
        sl.resolve('https://link.deezer.com/s/abc', get=get)
