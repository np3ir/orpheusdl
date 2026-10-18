import json
from types import SimpleNamespace

from utils import musiclink as ml


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


def resp(code=200, body=None, headers=None):
    return SimpleNamespace(status_code=code, json=lambda: body, headers=headers or {})


def ok(item):
    return resp(200, {'success': True, 'data': [item]})


TRACK = {'type': 'track', 'title': 'Eres Mia', 'artist': 'Romeo Santos', 'isrc': 'usrc11300001',
         'duration_ms': 241000,
         'platforms': {'apple_music': 'https://music.apple.com/song/1', 'deezer': 'https://www.deezer.com/track/9',
                       'tidal': 'https://tidal.com/browse/track/7'}}


def client(tmp_path, *responses, **kw):
    return ml.MusicLink('KEY', http=FakeHttp(*responses), cache_path=str(tmp_path / 'c.json'),
                        interval=0, sleep=lambda s: None, **kw)


def test_is_foreign():
    assert ml.is_foreign('https://music.apple.com/us/album/x/1')
    assert ml.is_foreign('music.youtube.com/watch?v=abc')
    assert not ml.is_foreign('https://tidal.com/browse/track/1')
    assert not ml.is_foreign('https://open.qobuz.com/track/1')
    assert not ml.is_foreign('https://open.spotify.com/track/1')


def test_resolve_sends_bearer_key_and_caches(tmp_path):
    c = client(tmp_path, ok(TRACK))
    item = c.resolve('https://music.apple.com/song/1')
    assert item['isrc'] == 'usrc11300001' and c._http.calls[0][2] == {'Authorization': 'Bearer KEY'}
    assert c._http.calls[0][1] == {'q': 'https://music.apple.com/song/1'}
    c2 = client(tmp_path)                       # no responses left: must come from the disk cache
    assert c2.resolve('https://music.apple.com/song/1')['title'] == 'Eres Mia'
    assert 'KEY' not in open(tmp_path / 'c.json', encoding='utf-8').read()


def test_translate_prefers_in_order_and_can_veto(tmp_path):
    c = client(tmp_path, ok(TRACK))
    r = ml.translate('x', c, prefer=('qobuz', 'tidal', 'deezer'))
    assert r['url'] == 'https://tidal.com/browse/track/7' and r['via'] == 'tidal' and r['isrc'] == 'USRC11300001'
    c2 = client(tmp_path / 'b', ok(TRACK))
    r2 = ml.translate('y', c2, prefer=('qobuz', 'tidal', 'deezer'), accept=lambda u: 'deezer' in u)
    assert r2['via'] == 'deezer'


def test_translate_isrc_only_track_and_nothing_usable(tmp_path):
    only = dict(TRACK, platforms={'apple_music': 'https://music.apple.com/song/1'})
    r = ml.translate('x', client(tmp_path, ok(only)))
    assert r['url'] is None and r['isrc'] == 'USRC11300001' and r['via'] == 'isrc'
    album = {'type': 'album', 'title': 'A', 'artist': 'B', 'platforms': {'apple_music': 'u'}}
    assert ml.translate('z', client(tmp_path / 'b', ok(album))) is None


def test_errors(tmp_path):
    import pytest
    with pytest.raises(ml.MusicLinkError):
        client(tmp_path, resp(401, {})).resolve('a')
    assert client(tmp_path / 'b', resp(404, {})).resolve('a') is None
    slow = client(tmp_path / 'c', resp(429, {}, {'Retry-After': '1'}), ok(TRACK))
    assert slow.resolve('a')['type'] == 'track'
    with pytest.raises(ml.MusicLinkError):
        client(tmp_path / 'd', resp(500), resp(500), resp(500)).resolve('a')


def test_api_key_from_env_or_settings(monkeypatch, tmp_path):
    nofile = str(tmp_path / 'no-key-file.txt')      # never read the user's real config/musiclink_key.txt
    monkeypatch.delenv('MUSICLINK_API_KEY', raising=False)
    assert ml.api_key({'modules': {'musiclink': {'api_key': ' abc '}}}, key_file=nofile) == 'abc'
    assert ml.api_key({}, key_file=nofile) == ''
    monkeypatch.setenv('MUSICLINK_API_KEY', 'envkey')
    assert ml.api_key({}, key_file=nofile) == 'envkey'


def test_api_key_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv('MUSICLINK_API_KEY', raising=False)
    f = tmp_path / 'k.txt'
    f.write_text('  filekey  ' + chr(10) + 'ignored second line' + chr(10), encoding='utf-8')
    assert ml.api_key({}, key_file=str(f)) == 'filekey'
    assert ml.api_key({}, key_file=str(tmp_path / 'missing.txt')) == ''
    monkeypatch.setenv('MUSICLINK_API_KEY', 'envwins')
    assert ml.api_key({}, key_file=str(f)) == 'envwins'
