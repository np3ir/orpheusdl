from requests.adapters import HTTPAdapter

from utils import utils


def _sent_timeouts(monkeypatch, **kwargs):
    seen = []
    monkeypatch.setattr(HTTPAdapter, 'send', lambda self, request, **kw: seen.append(kw.get('timeout')))
    adapter = utils.TimeoutHTTPAdapter()
    adapter.send(object(), **kwargs)
    return seen


def test_default_timeout_is_added(monkeypatch):
    assert _sent_timeouts(monkeypatch, timeout=None) == [utils.DEFAULT_HTTP_TIMEOUT]
    assert _sent_timeouts(monkeypatch) == [utils.DEFAULT_HTTP_TIMEOUT]


def test_explicit_timeout_wins(monkeypatch):
    assert _sent_timeouts(monkeypatch, timeout=5) == [5]


def test_sessions_mount_the_timeout_adapter():
    session = utils.create_requests_session()
    assert isinstance(session.get_adapter('https://api.deezer.com/'), utils.TimeoutHTTPAdapter)
