import json
from types import SimpleNamespace

from utils import genre_lookup as gl


class FakeDeezer:
    def __init__(self, tracks=None, albums=None):
        self.tracks = tracks or {}
        self.albums = albums or {}
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path.startswith('/track/isrc:'):
            return self.tracks.get(path.split(':', 1)[1])
        if path.startswith('/album/'):
            return self.albums.get(path.rsplit('/', 1)[1])
        raise AssertionError(path)


class FakeSpotify:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def artist_genres_by_isrc(self, isrc):
        self.calls.append(isrc)
        return self.result


def make(tmp_path, deezer, spotify=None, use_spotify=True):
    return gl.GenreLookup(deezer_get=deezer, spotify=spotify, use_spotify=use_spotify,
                          cache_path=str(tmp_path / 'g.json'), interval=0, sleep=lambda s: None)


def album(*names):
    return {'genres': {'data': [{'name': n} for n in names]}}


def test_deezer_genres_are_mapped_to_library_vocabulary(tmp_path):
    dz = FakeDeezer({'AAA': {'album': {'id': 7}}}, {'7': album('Rap/Hip Hop', 'Latin Music', 'Pop')})
    lk = make(tmp_path, dz)
    assert lk.genres('aaa') == ['Hip-Hop/Rap', 'Latin', 'Pop']


def test_album_genres_are_cached_across_tracks_and_isrcs_across_runs(tmp_path):
    dz = FakeDeezer({'AAA': {'album': {'id': 7}}, 'BBB': {'album': {'id': 7}}}, {'7': album('Pop')})
    lk = make(tmp_path, dz)
    lk.genres('AAA')
    lk.genres('BBB')
    assert [c for c in dz.calls if c.startswith('/album/')] == ['/album/7']   # album asked once
    n = len(dz.calls)
    lk.genres('AAA')
    assert len(dz.calls) == n                                                # ISRC cached
    lk.save()
    lk2 = make(tmp_path, FakeDeezer())                                       # fresh object, same file
    assert lk2.genres('AAA') == ['Pop']


def test_spotify_is_only_used_when_deezer_has_nothing(tmp_path):
    sp = FakeSpotify(['reggaeton colombiano', 'latin pop'])
    dz = FakeDeezer({'AAA': {'album': {'id': 7}}}, {'7': album('Pop')})
    assert make(tmp_path, dz, sp).genres('AAA') == ['Pop']
    assert sp.calls == []
    sp2 = FakeSpotify(['reggaeton colombiano', 'latin pop'])
    assert make(tmp_path / 'x', FakeDeezer(), sp2).genres('ZZZ') == ['Reggaeton', 'Latin']
    assert sp2.calls == ['ZZZ']


def test_spotify_disabled_or_unavailable_gives_empty(tmp_path):
    sp = FakeSpotify(['pop'])
    assert make(tmp_path, FakeDeezer(), sp, use_spotify=False).genres('ZZZ') == []
    assert sp.calls == []
    assert make(tmp_path / 'y', FakeDeezer(), FakeSpotify(None)).genres('ZZZ') == []   # blocked -> None


def test_failures_never_raise(tmp_path):
    def boom(path):
        raise RuntimeError('network down')
    assert make(tmp_path, boom).genres('AAA') == []
    assert make(tmp_path / 'z', FakeDeezer()).genres('') == []


def test_normalize_spotify_collapses_micro_genres():
    assert gl.normalize_spotify(['trap latino', 'rap']) == ['Latin', 'Hip-Hop/Rap']
    assert gl.normalize_spotify(['indie rock', 'dream pop']) == ['Alternative & Indie', 'Pop']
    assert gl.normalize_spotify(['zzzz unknown']) == []


def test_fill_genres_only_when_empty_and_isrc_present(tmp_path, monkeypatch):
    lk = make(tmp_path, FakeDeezer({'AAA': {'album': {'id': 1}}}, {'1': album('Rock')}))
    monkeypatch.setattr(gl, 'shared', lambda: lk)
    ti = SimpleNamespace(tags=SimpleNamespace(genres=[], isrc='AAA'))
    assert gl.fill_genres(ti) and ti.tags.genres == ['Rock']
    has = SimpleNamespace(tags=SimpleNamespace(genres=['Jazz'], isrc='AAA'))
    assert not gl.fill_genres(has) and has.tags.genres == ['Jazz']
    no_isrc = SimpleNamespace(tags=SimpleNamespace(genres=[], isrc=None))
    assert not gl.fill_genres(no_isrc)


def test_disabled_by_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(gl, '_shared', None)
    monkeypatch.setattr(gl, '_settings', lambda: {'global': {'general': {'genre_fallback': False}}})
    assert gl.shared() is None
    monkeypatch.setattr(gl, '_shared', None)


def test_spotify_derived_genres_are_never_persisted(tmp_path):
    sp = FakeSpotify(['salsa colombiana'])
    lk = make(tmp_path, FakeDeezer(), sp)
    assert lk.genres('ZZZ') == ['Salsa']
    assert lk.genres('ZZZ') == ['Salsa'] and sp.calls == ['ZZZ']     # served from memory
    lk.save()
    cache = json.load(open(tmp_path / 'g.json', encoding='utf-8')) if (tmp_path / 'g.json').exists() else {}
    assert 'isrc:ZZZ' not in cache
    assert make(tmp_path, FakeDeezer()).genres('ZZZ') == []          # a new run knows nothing of it


def test_spotify_source_is_off_by_default(monkeypatch):
    monkeypatch.setattr(gl, '_shared', None)
    monkeypatch.setattr(gl, '_settings', lambda: {'modules': {'spotify': {'client_id': 'x', 'client_secret': 'y'}}})
    lk = gl.shared()
    assert lk is not None and lk._spotify is None
    monkeypatch.setattr(gl, '_shared', None)


def test_deezer_album_level_names_are_mapped():
    for raw, want in [('Techno/House', 'Electronic'), ('Film Scores', 'Film Soundtracks'), ('Indie Pop', 'Alternative & Indie'),
                      ('Dancehall/Ragga', 'Dancehall'), ('Contemporary R&B', 'R&B'), ('Reggaeton', 'Reggaeton'),
                      ('Salsa', 'Salsa'), ('International Pop', 'International Pop')]:
        assert gl.normalize_deezer(raw) == want
