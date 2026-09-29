"""Offline tests for abq's TIDAL guest GET retry policy (no network)."""
import json

import pytest
import requests

abq = pytest.importorskip('artist_best_quality')


def resp(code=200, body=None, raw=None):
    r = requests.Response()
    r.status_code = code
    r._content = raw.encode() if raw is not None else json.dumps(body or {}).encode()
    return r


@pytest.fixture
def env(monkeypatch):
    waits, calls, auths = [], [], []
    monkeypatch.setitem(abq._TIDAL_GUEST, 'tok', 'tok1')
    monkeypatch.setattr(abq.time, 'sleep', waits.append)
    monkeypatch.setattr(abq.random, 'random', lambda: 0.0)  # jitter factor 0.8

    def auth():
        auths.append(1)
        abq._TIDAL_GUEST['tok'] = f'tok{len(auths) + 1}'
        return abq._TIDAL_GUEST['tok']
    monkeypatch.setattr(abq, '_tidal_guest_auth', auth)

    def install(*responses):
        queue = list(responses)

        def get(url, headers=None, timeout=None):
            calls.append((url, headers['Authorization']))
            r = queue.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(abq.requests, 'get', get)
    return install, waits, calls, auths


def test_backoff_formula_matches_sdk():
    assert abq._tidal_backoff(0.5, 16.0, 0) == pytest.approx(0.5 * 0.8, abs=0.1)
    for n in range(12):
        d = abq._tidal_backoff(0.5, 16.0, n)
        assert 0.5 * 2 ** n * 0.8 - 1e-9 <= d or d == 16.0
        assert d <= 16.0


def test_429_retried_with_growing_jittered_delay(env):
    install, waits, calls, _ = env
    install(resp(429), resp(503), resp(429), resp(200, {'data': [1]}))
    assert abq._tidal_guest_get('/x') == {'data': [1]}
    assert waits == pytest.approx([0.4, 0.8, 1.6])


def test_status_retries_exhausted_returns_none(env):
    install, waits, calls, _ = env
    install(*[resp(429)] * 6)
    assert abq._tidal_guest_get('/x') is None
    assert len(calls) == 6 and len(waits) == 5
    assert max(waits) <= 16.0


def test_network_errors_use_their_own_budget(env):
    install, waits, calls, _ = env
    install(requests.ConnectionError('reset'), requests.ConnectionError('reset'), resp(429),
            resp(200, {'ok': True}))
    assert abq._tidal_guest_get('/x') == {'ok': True}
    assert waits == pytest.approx([0.8, 1.6, 0.4])  # network n=0,1 then status n=0


def test_timeouts_use_the_sdk_timeout_policy(env):
    install, waits, calls, _ = env
    install(*[requests.Timeout('slow')] * 4)
    assert abq._tidal_guest_get('/x') is None
    assert len(calls) == 4                           # 3 retries, as in the SDK
    assert waits == pytest.approx([6.4, 12.8, 25.6])  # 8 s * 2^n * 0.8, cap 32 s


def test_404_is_not_retried(env):
    install, waits, calls, _ = env
    install(resp(404))
    assert abq._tidal_guest_get('/albums/nope') is None
    assert len(calls) == 1 and waits == []


def test_401_renews_token_once(env):
    install, waits, calls, auths = env
    install(resp(401), resp(200, {'ok': 1}))
    assert abq._tidal_guest_get('/x') == {'ok': 1}
    assert [c[1] for c in calls] == ['Bearer tok1', 'Bearer tok2'] and len(auths) == 1


def test_repeated_401_gives_up(env):
    install, waits, calls, auths = env
    install(resp(401), resp(401))
    assert abq._tidal_guest_get('/x') is None
    assert len(auths) == 1 and len(calls) == 2
