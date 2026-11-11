"""Offline tests (local HTTP server) for download resume (Range) and signed-URL renewal."""
import asyncio
import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import pytest
import requests

from utils import utils

_REAL_SLEEP = time.sleep  # the fixture below patches time.sleep globally
DATA = bytes(range(256)) * 400  # 102400 bytes, position-dependent so a bad splice is visible


class Server:
    """Serves DATA at /file?t=<token>. Behaviour is set per test."""

    def __init__(self):
        self.requests = []      # (token, range_header)
        self.drop_first_after = None   # cut the first full response after N bytes
        self.honor_range = True
        self.good_tokens = {'ok'}
        self.statuses = []      # scripted statuses consumed before normal serving
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                token = self.path.split('t=')[-1]
                rng = self.headers.get('Range')
                server.requests.append((token, rng))
                if server.statuses:
                    self.send_response(server.statuses.pop(0))
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if token not in server.good_tokens:
                    self.send_response(403)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                start = 0
                if rng and server.honor_range:
                    start = int(rng.split('=')[1].rstrip('-'))
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {start}-{len(DATA) - 1}/{len(DATA)}')
                else:
                    self.send_response(200)
                body = DATA[start:]
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                cut = server.drop_first_after
                if cut is not None and len(server.requests) == 1:
                    self.wfile.write(body[:cut])
                    self.wfile.flush()
                    _REAL_SLEEP(0.3)  # let the client consume what it got before the cut
                    self.connection.close()
                    return
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, token='ok'):
        return f'http://127.0.0.1:{self.httpd.server_address[1]}/file?t={token}'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setattr(utils.time, 'sleep', lambda s: None)
    s = Server()
    yield s
    s.close()


def run_async(coro_fn):
    async def main():
        async with aiohttp.ClientSession() as session:
            return await coro_fn(session)
    return asyncio.run(main())


def digest(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


WANT = hashlib.sha256(DATA).hexdigest()


# ---- _resume_plan --------------------------------------------------------------------

def test_plan_fresh_request_uses_content_length():
    assert utils._resume_plan(200, {'content-length': '10'}, 0, None) == (0, 10)


def test_plan_server_ignored_range_rewrites():
    assert utils._resume_plan(200, {'content-length': '10'}, 4, None) == (0, 10)


def test_plan_valid_206_resumes():
    h = {'content-range': 'bytes 4-9/10', 'content-length': '6'}
    assert utils._resume_plan(206, h, 4, None) == (4, 10)


def test_plan_206_at_wrong_offset_is_unusable():
    h = {'content-range': 'bytes 0-9/10', 'content-length': '10'}
    assert utils._resume_plan(206, h, 4, None) == (None, None)


def test_plan_206_of_a_different_resource_is_unusable():
    h = {'content-range': 'bytes 4-19/20', 'content-length': '16'}
    assert utils._resume_plan(206, h, 4, 10) == (None, None)


# ---- async ---------------------------------------------------------------------------

def test_async_resumes_after_cut_connection(srv, tmp_path):
    srv.drop_first_after = 30000
    out = tmp_path / 'a.flac'
    run_async(lambda s: utils.download_file_async(s, srv.url(), str(out)))
    assert digest(out) == WANT
    assert srv.requests[0][1] is None
    start = int(srv.requests[1][1].split('=')[1].rstrip('-'))
    assert 0 < start <= 30000


def test_async_server_without_range_support_still_completes(srv, tmp_path):
    srv.drop_first_after = 30000
    srv.honor_range = False
    out = tmp_path / 'a.flac'
    run_async(lambda s: utils.download_file_async(s, srv.url(), str(out)))
    assert digest(out) == WANT


def test_async_expired_url_is_renewed(srv, tmp_path):
    calls = []

    def refresh():
        calls.append(1)
        return srv.url('ok')
    out = tmp_path / 'a.flac'
    run_async(lambda s: utils.download_file_async(s, srv.url('old'), str(out), refresh_url=refresh))
    assert digest(out) == WANT
    assert len(calls) == 1
    assert [t for t, _ in srv.requests] == ['old', 'ok']


def test_async_expired_url_without_refresh_fails_at_once(srv, tmp_path):
    out = tmp_path / 'a.flac'
    with pytest.raises(aiohttp.ClientResponseError) as ei:
        run_async(lambda s: utils.download_file_async(s, srv.url('old'), str(out)))
    assert ei.value.status == 403
    assert len(srv.requests) == 1          # no identical retries of a dead URL
    assert not out.exists()


def test_async_refresh_that_gives_nothing_fails(srv, tmp_path):
    out = tmp_path / 'a.flac'
    with pytest.raises(aiohttp.ClientResponseError):
        run_async(lambda s: utils.download_file_async(
            s, srv.url('old'), str(out), refresh_url=lambda: None))
    assert not out.exists()


def test_async_renewal_is_bounded(srv, tmp_path):
    out = tmp_path / 'a.flac'
    with pytest.raises(aiohttp.ClientResponseError):
        run_async(lambda s: utils.download_file_async(
            s, srv.url('old'), str(out), refresh_url=lambda: srv.url('still-bad')))
    assert len(srv.requests) == 3          # first try + 2 renewals


def test_async_retries_transient_5xx(srv, tmp_path):
    srv.statuses = [503, 503]
    out = tmp_path / 'a.flac'
    run_async(lambda s: utils.download_file_async(s, srv.url(), str(out)))
    assert digest(out) == WANT


# ---- sync ----------------------------------------------------------------------------

def test_sync_resumes_after_cut_connection(srv, tmp_path):
    srv.drop_first_after = 30000
    out = tmp_path / 's.flac'
    assert utils.download_file(srv.url(), str(out)) == str(out)
    assert digest(out) == WANT
    start = int(srv.requests[1][1].split('=')[1].rstrip('-'))
    assert 0 < start <= 30000


def test_sync_expired_url_is_renewed(srv, tmp_path):
    out = tmp_path / 's.flac'
    assert utils.download_file(srv.url('old'), str(out), refresh_url=lambda: srv.url('ok')) == str(out)
    assert digest(out) == WANT
    assert [t for t, _ in srv.requests] == ['old', 'ok']


def test_sync_expired_url_without_refresh_fails_at_once(srv, tmp_path):
    out = tmp_path / 's.flac'
    with pytest.raises(requests.exceptions.HTTPError):
        utils.download_file(srv.url('old'), str(out))
    assert len(srv.requests) == 1
    assert not out.exists()
    assert not list(tmp_path.iterdir())    # no stray .part file left behind


def test_sync_server_without_range_support_still_completes(srv, tmp_path):
    srv.drop_first_after = 30000
    srv.honor_range = False
    out = tmp_path / 's.flac'
    utils.download_file(srv.url(), str(out))
    assert digest(out) == WANT


def test_sync_tls_error_is_not_retried(monkeypatch, tmp_path):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.exceptions.SSLError('bad cert')
    monkeypatch.setattr(utils.r_session, 'get', boom)
    monkeypatch.setattr(utils.time, 'sleep', lambda s: None)
    with pytest.raises(requests.exceptions.SSLError):
        utils.download_file('https://example.invalid/x.flac', str(tmp_path / 'x.flac'))
    assert len(calls) == 1
