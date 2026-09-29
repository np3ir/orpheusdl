"""Offline tests for utils/deezer_public.py and abq's Deezer public lookups (no network)."""
import json
from types import SimpleNamespace

import pytest
import requests

import utils.deezer_public as dp


def resp(body=None, code=200, raw=None):
    r = requests.Response()
    r.status_code = code
    r._content = raw.encode() if raw is not None else json.dumps(body).encode()
    return r


def api_error(code, type_='Exception', msg='x'):
    return resp({'error': {'type': type_, 'message': msg, 'code': code}})


ACCESS_DENIED = resp(raw='<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD></HTML>', code=403)


class FakeHttp:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def no_sleep(_):
    pass


def test_success_returns_body():
    http = FakeHttp(resp({'id': 3135553, 'isrc': 'GBDUW0000053'}))
    assert dp.get('/track/isrc:GBDUW0000053', http=http, sleep=no_sleep)['id'] == 3135553
    assert http.urls == ['https://api.deezer.com/track/isrc:GBDUW0000053']


def test_data_not_found_is_none_without_retry():
    http = FakeHttp(api_error(800, 'DataException', 'no data'))
    assert dp.get('/track/isrc:XX', http=http, sleep=no_sleep) is None
    assert len(http.urls) == 1


@pytest.mark.parametrize('first', [api_error(4, msg='Quota limit exceeded'),
                                   api_error(700, msg='Service busy'),
                                   ACCESS_DENIED,
                                   requests.ConnectionError('reset')])
def test_temporary_failures_are_retried(first):
    http = FakeHttp(first, resp({'id': 1}))
    assert dp.get('/track/1', http=http, sleep=no_sleep) == {'id': 1}
    assert len(http.urls) == 2


def test_persistent_failure_raises_instead_of_not_found():
    http = FakeHttp(*[api_error(4)] * 4)
    with pytest.raises(dp.DeezerLookupError, match='after 4 attempts'):
        dp.get('/track/1', http=http, sleep=no_sleep)


def test_other_api_error_raises_immediately():
    http = FakeHttp(api_error(500, 'ParameterException', 'Wrong parameter'))
    with pytest.raises(dp.DeezerLookupError, match='ParameterException 500'):
        dp.get('/track/isrc:bad', http=http, sleep=no_sleep)
    assert len(http.urls) == 1


def test_backoff_grows():
    waits = []
    http = FakeHttp(api_error(4), api_error(4), resp({'id': 1}))
    dp.get('/track/1', http=http, backoff=2.0, sleep=waits.append)
    assert waits == [2.0, 4.0]


# --- abq integration: uses the gated module session, warns on failure -------- #
abq = pytest.importorskip('artist_best_quality')


class GatedSession(requests.Session):
    def __init__(self, *responses):
        super().__init__()
        self.fake = FakeHttp(*responses)

    def get(self, url, timeout=None, **kw):
        return self.fake.get(url, timeout=timeout)


def core_with_deezer(session):
    return SimpleNamespace(_modules={'deezer': SimpleNamespace(session=SimpleNamespace(s=session))})


def test_probe_uses_module_session(monkeypatch):
    s = GatedSession(resp({'id': 42, 'artist': {'id': 7}}))
    assert abq.probe_isrc(core_with_deezer(s), 'deezer', 'GBDUW0000053') == ('42', 16, 44.1)
    assert s.fake.urls == ['https://api.deezer.com/track/isrc:GBDUW0000053']


def test_probe_failure_is_logged_not_silent(monkeypatch, capsys):
    monkeypatch.setattr(dp.time, 'sleep', no_sleep)
    monkeypatch.setattr(dp, 'get', lambda path, http=None: (_ for _ in ()).throw(
        dp.DeezerLookupError(f'{path}: QUOTA 4')))
    s = GatedSession()
    assert abq.probe_isrc(core_with_deezer(s), 'deezer', 'GBDUW0000053') is None
    assert 'lookup FAILED' in capsys.readouterr().out


def test_artist_id_by_isrc(monkeypatch):
    s = GatedSession(resp({'id': 42, 'artist': {'id': 27}}))
    assert abq._artist_id_by_isrc(core_with_deezer(s), 'deezer', 'X') == '27'
