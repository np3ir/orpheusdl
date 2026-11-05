import pytest

import artist_best_quality as abq


def cfg(**kw):
    return abq.abq_config({'global': {'artist_best_quality': kw}})


def test_missing_services_are_appended_by_default():
    assert cfg(prefer_order=['tidal', 'deezer'])['prefer_order'] == ['tidal', 'deezer', 'qobuz']


def test_disabled_service_is_never_used():
    out = cfg(prefer_order=['qobuz', 'tidal', 'deezer'], disabled_services=['Qobuz'])
    assert out['prefer_order'] == ['tidal', 'deezer']


def test_disabling_every_service_is_refused():
    with pytest.raises(SystemExit):
        cfg(disabled_services=list(abq.ALL_SERVICES))


def _dash(rate, codec='flac'):
    import base64
    xml = f'<MPD><Period><AdaptationSet contentType="audio"><Representation codecs="{codec}" audioSamplingRate="{rate}"/></AdaptationSet></Period></MPD>'
    return {'manifestMimeType': 'application/dash+xml', 'audioQuality': 'HI_RES_LOSSLESS',
            'manifest': base64.b64encode(xml.encode()).decode()}


def test_tidal_stream_quality_reads_real_rate():
    import artist_best_quality as abq
    assert abq.tidal_stream_quality(_dash(192000)) == (24, 192.0)
    assert abq.tidal_stream_quality(_dash(96000, codec='mp4a.40.2')) is None
    assert abq.tidal_stream_quality({'manifestMimeType': 'application/vnd.tidal.bts'}) is None
    d = _dash(44100); d['audioQuality'] = 'LOSSLESS'
    assert abq.tidal_stream_quality(d) == (16, 44.1)


def _fake_core(stream):
    class Sess:
        default = None

        def get_stream_url(self, tid, q):
            return stream(tid, q)

    class Mod:
        session = Sess()
        available_sessions = ['TV', 'MOBILE_DEFAULT']

        def _ensure_credentials(self, force=False):
            pass

    class Core:
        def module(self, name):
            return Mod()

    return Core()


def test_refine_makes_tidal_192_beat_qobuz_96():
    import artist_best_quality as abq
    union = {'X': {'qobuz': ('1', 24, 96.0), 'tidal': ('2', 24, 96.0)},
             'Y': {'qobuz': ('3', 16, 44.1), 'tidal': ('4', 16, 44.1)}}
    abq.refine_tidal_quality(_fake_core(lambda t, q: _dash(192000)), union)
    assert union['X']['tidal'][1:3] == (24, 192.0)
    assert union['Y']['tidal'] == ('4', 16, 44.1)   # untouched: no hi-res involved
    assert abq.choose_service(union['X'], 'best', ['qobuz', 'tidal', 'deezer'])[0] == 'tidal'


def test_refine_keeps_proxy_on_failure():
    import artist_best_quality as abq

    def boom(t, q):
        raise RuntimeError('boom')

    union = {'X': {'qobuz': ('1', 24, 96.0), 'tidal': ('2', 24, 96.0)}}
    abq.refine_tidal_quality(_fake_core(boom), union)
    assert union['X']['tidal'] == ('2', 24, 96.0)
