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


def _attrs(rate, codec='flac', formats=('FLAC_HIRES',)):
    import base64
    xml = (f'<MPD><Period><AdaptationSet contentType="audio"><Representation codecs="{codec}" '
           f'audioSamplingRate="{rate}"/></AdaptationSet></Period></MPD>')
    return {'uri': 'data:application/dash+xml;base64,' + base64.b64encode(xml.encode()).decode(),
            'formats': list(formats)}


def test_tidal_manifest_quality_reads_real_rate():
    import artist_best_quality as abq
    assert abq.tidal_manifest_quality(_attrs(192000)) == (24, 192.0)
    assert abq.tidal_manifest_quality(_attrs(48000)) == (24, 48.0)
    assert abq.tidal_manifest_quality(_attrs(44100, formats=('FLAC',))) == (16, 44.1)
    assert abq.tidal_manifest_quality(_attrs(96000, codec='mp4a.40.2')) is None
    assert abq.tidal_manifest_quality({'uri': 'https://x'}) is None


def _with_manifests(monkeypatch, fn):
    import artist_best_quality as abq
    abq._TIDAL_MANIFEST_CACHE.clear()
    monkeypatch.setattr(abq, '_tidal_guest_get', fn)


def test_refine_makes_tidal_192_beat_qobuz_96(monkeypatch):
    import artist_best_quality as abq
    _with_manifests(monkeypatch, lambda path: {'data': {'attributes': _attrs(192000)}})
    union = {'X': {'qobuz': ('1', 24, 96.0), 'tidal': ('2', 24, 96.0)},
             'Y': {'qobuz': ('3', 16, 44.1), 'tidal': ('4', 16, 44.1)}}
    abq.refine_tidal_quality(union)
    assert union['X']['tidal'][1:3] == (24, 192.0)
    assert union['Y']['tidal'] == ('4', 16, 44.1)   # untouched: no hi-res involved
    assert abq.choose_service(union['X'], 'best', ['qobuz', 'tidal', 'deezer'])[0] == 'tidal'


def test_refine_demotes_tidal_48_below_qobuz_96(monkeypatch):
    import artist_best_quality as abq
    _with_manifests(monkeypatch, lambda path: {'data': {'attributes': _attrs(48000)}})
    union = {'X': {'qobuz': ('1', 24, 96.0), 'tidal': ('2', 24, 96.0)}}
    abq.refine_tidal_quality(union)
    assert abq.choose_service(union['X'], 'best', ['tidal', 'qobuz'])[0] == 'qobuz'


def test_refine_skips_when_other_service_already_192(monkeypatch):
    import artist_best_quality as abq
    calls = []
    _with_manifests(monkeypatch, lambda path: calls.append(path))
    union = {'X': {'qobuz': ('1', 24, 192.0), 'tidal': ('2', 24, 96.0)}}
    abq.refine_tidal_quality(union)
    assert not calls and union['X']['tidal'] == ('2', 24, 96.0)


def test_refine_keeps_proxy_on_failure(monkeypatch):
    import artist_best_quality as abq
    _with_manifests(monkeypatch, lambda path: None)
    union = {'X': {'qobuz': ('1', 24, 96.0), 'tidal': ('2', 24, 96.0)}}
    abq.refine_tidal_quality(union)
    assert union['X']['tidal'] == ('2', 24, 96.0)
