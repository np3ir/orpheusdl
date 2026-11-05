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
