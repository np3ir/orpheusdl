"""Offline tests for Tidal mix links (tidal.com/mix/<id>) in abq."""
import pytest

import artist_best_quality as abq

MIX = '002eb4d20492751cbd38cfacec9390'


# ---- parse_media_url -----------------------------------------------------------------

@pytest.mark.parametrize('url', [
    f'https://tidal.com/mix/{MIX}',
    f'https://tidal.com/mix/{MIX}/',
    f'https://listen.tidal.com/mix/{MIX}',
    f'https://tidal.com/browse/mix/{MIX}',
    f'tidal.com/mix/{MIX}',
    f'https://tidal.com/mix/{MIX}?u=1',
])
def test_tidal_mix_links_are_understood(url):
    assert abq.parse_media_url(url) == ('tidal', 'mix', MIX)


def test_mix_on_another_service_is_refused():
    with pytest.raises(SystemExit) as e:
        abq.parse_media_url(f'https://www.deezer.com/mix/{MIX}')
    assert 'only exist on Tidal' in str(e.value)


@pytest.mark.parametrize('url', ['https://tidal.com/mix', 'https://tidal.com/mix/', 'https://tidal.com/mix/12'])
def test_mix_link_without_a_usable_id_is_refused(url):
    with pytest.raises(SystemExit):
        abq.parse_media_url(url)


def test_other_link_types_still_parse():
    assert abq.parse_media_url('https://tidal.com/artist/10411') == ('tidal', 'artist', '10411')
    assert abq.parse_media_url('https://tidal.com/browse/track/96594261') == ('tidal', 'track', '96594261')
    assert abq.parse_media_url('https://tidal.com/browse/album/123') == ('tidal', 'album', '123')
    assert abq.parse_media_url('https://www.deezer.com/en/playlist/908622995') == ('deezer', 'playlist', '908622995')
    assert abq.parse_media_url('https://www.qobuz.com/us-en/album/mix/0000') == ('qobuz', 'album', '0000')


def test_unsupported_link_message_mentions_mix():
    with pytest.raises(SystemExit) as e:
        abq.parse_media_url('https://tidal.com/browse/video/123')
    assert 'mix' in str(e.value)


# ---- reading the ISRCs of a mix ------------------------------------------------------

class FakeTidal:
    """Serves `total` items from `.../items`, 100 per page, like Tidal's v1 API."""

    def __init__(self, items, fail_first=0):
        self.items = items
        self.calls = []
        self.fail_first = fail_first

    def _get(self, path, params):
        self.calls.append((path, dict(params)))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError('boom')
        off, lim = params['offset'], params['limit']
        return {'limit': lim, 'offset': off, 'totalNumberOfItems': len(self.items),
                'items': self.items[off:off + lim]}


class FakeCore:
    def __init__(self, session):
        self._s = session

    def session(self, name):
        assert name == 'tidal'
        return self._s


def track(isrc):
    return {'type': 'track', 'item': {'id': 1, 'title': 't', 'isrc': isrc}}


def test_mix_isrcs_are_read_from_the_mix_items_endpoint():
    s = FakeTidal([track('USWI18800036'), track('es36i1300218')])
    out = abq.source_isrcs(FakeCore(s), 'tidal', 'mix', MIX)
    assert out == {'USWI18800036', 'ES36I1300218'}
    assert s.calls[0][0] == f'mixes/{MIX}/items'


def test_mix_pages_are_all_read_and_non_tracks_skipped():
    items = [track(f'AAA{n:09d}') for n in range(230)]
    items.insert(5, {'type': 'video', 'item': {'id': 9, 'title': 'clip'}})   # no ISRC
    items.insert(7, {'type': 'track', 'item': {'id': 2, 'title': 'no isrc', 'isrc': None}})
    items.insert(9, track('aaa000000003'))                                    # duplicate (case-insensitive)
    s = FakeTidal(items)
    out = abq.source_isrcs(FakeCore(s), 'tidal', 'mix', MIX)
    assert len(out) == 230
    assert [c[1]['offset'] for c in s.calls] == [0, 100, 200]


def test_mix_read_gives_up_after_repeated_failures_and_keeps_what_it_has():
    s = FakeTidal([track('AAA000000001')], fail_first=99)
    out = abq.source_isrcs(FakeCore(s), 'tidal', 'mix', MIX)
    assert out == set()
    assert len(s.calls) == 4


def test_playlists_still_use_the_playlist_endpoint():
    s = FakeTidal([track('BBB000000001')])
    out = abq.source_isrcs(FakeCore(s), 'tidal', 'playlist', 'abc-123')
    assert out == {'BBB000000001'}
    assert s.calls[0][0] == 'playlists/abc-123/items'
