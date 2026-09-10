"""Tests for Deliverable 1 (immediate fixes):

1. The ``time`` UnboundLocalError in ``Downloader._concurrent_download_tracks``'s
   sequential branch (``concurrent_downloads <= 1``) is gone.
2. TLS validation is restored on BOTH download routes (no ``verify=False`` /
   ``ssl=False``); a certificate error propagates without any insecure fallback.
3. The synchronous route sets explicit (connect, read) timeouts and closes the
   response via a context manager, and cleans up a partial file on failure.

All network is simulated - no real connections, credentials, or downloads.
"""

import asyncio
import os
import sys

import pytest
import requests

# Make the project root importable when run directly or via pytest.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import utils.utils as uu  # noqa: E402
from orpheus.music_downloader import Downloader  # noqa: E402
from orpheus.music_downloader import _clean_track_title_for_artists  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The `time` fix: sequential branch runs with concurrent_downloads=1
# ---------------------------------------------------------------------------

def test_sequential_branch_runs_without_unbound_local_time():
    """The sequential branch must run a simulated download and return results,
    never raising `UnboundLocalError: ... 'time' ...` (the regression)."""
    dl = object.__new__(Downloader)
    dl.print = lambda *a, **k: None
    dl.service_name = None  # falsy -> skips the tidal-throttle import branch

    calls = []

    def fake_download_track(**kwargs):
        calls.append(kwargs)
        return f"DOWNLOADED:{kwargs.get('track_id')}"

    dl.download_track = fake_download_track

    track_list = ["t1", "t2"]
    download_args_list = [{"track_id": 1}, {"track_id": 2}]

    results = dl._concurrent_download_tracks(track_list, download_args_list, concurrent_downloads=1)

    assert results == [(0, "DOWNLOADED:1", None), (1, "DOWNLOADED:2", None)]
    assert calls == [{"track_id": 1}, {"track_id": 2}]
    # total_download_time is measured via the module-level `time` module.
    assert isinstance(dl.total_download_time, float)
    assert dl.total_download_time >= 0.0


def test_sequential_branch_captures_per_track_exceptions():
    """A failing track is recorded as (i, None, exc); the run still completes."""
    dl = object.__new__(Downloader)
    dl.print = lambda *a, **k: None
    dl.service_name = None

    boom = RuntimeError("simulated failure")

    def fake_download_track(**kwargs):
        if kwargs.get("track_id") == 2:
            raise boom
        return "OK"

    dl.download_track = fake_download_track

    results = dl._concurrent_download_tracks(["a", "b"], [{"track_id": 1}, {"track_id": 2}], 1)

    assert results[0] == (0, "OK", None)
    assert results[1][0] == 1 and results[1][1] is None and results[1][2] is boom


def test_featured_artist_is_removed_from_title_when_already_in_artists():
    assert _clean_track_title_for_artists(
        "Eres Mi Bendicion (feat. Alex Zurdo)", ["Funky", "Alex Zurdo"]
    ) == "Eres Mi Bendicion"


def test_unknown_featured_artist_and_legitimate_title_text_are_preserved():
    title = "Song (feat. Unknown Artist)"
    assert _clean_track_title_for_artists(title, ["Funky"]) == title
    assert _clean_track_title_for_artists("Alex Zurdo's Song", ["Alex Zurdo"]) == "Alex Zurdo's Song"


# ---------------------------------------------------------------------------
# Fakes for the synchronous requests path
# ---------------------------------------------------------------------------

class _FakeSyncResponse:
    def __init__(self, data=b"AUDIODATA", headers=None, iter_exc=None):
        self._data = data
        self.headers = headers or {"content-length": str(len(data))}
        self._iter_exc = iter_exc
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1024):
        if self._iter_exc is not None:
            raise self._iter_exc
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]

    def close(self):
        self.closed = True


class _FakeSyncSession:
    def __init__(self, response=None, get_exc=None):
        self._response = response
        self._get_exc = get_exc
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        if self._get_exc is not None:
            raise self._get_exc
        return self._response


# ---------------------------------------------------------------------------
# 2. Synchronous route: TLS on + timeout + response closed + correct bytes
# ---------------------------------------------------------------------------

def test_sync_download_uses_tls_and_timeout_and_closes_response(monkeypatch, tmp_path):
    resp = _FakeSyncResponse(data=b"hello-world-flac-bytes")
    session = _FakeSyncSession(response=resp)
    monkeypatch.setattr(uu, "r_session", session)

    dest = tmp_path / "track.flac"
    out = uu.download_file("https://example/track.flac", str(dest), skip_if_exists=False)

    assert out == str(dest)
    assert dest.read_bytes() == b"hello-world-flac-bytes"  # explicit loop wrote all chunks

    kwargs = session.calls[0]["kwargs"]
    assert kwargs.get("verify", True) is not False, "TLS validation must not be disabled"
    assert "verify" not in kwargs, "verify=False was removed; default (enabled) is used"
    assert kwargs.get("timeout") == (10, 60), "explicit (connect, read) timeout required"
    assert resp.closed is True, "response must be closed via the context manager"


def test_sync_download_certificate_error_propagates_without_insecure_fallback(monkeypatch, tmp_path):
    session = _FakeSyncSession(get_exc=requests.exceptions.SSLError("certificate verify failed"))
    monkeypatch.setattr(uu, "r_session", session)

    dest = tmp_path / "track.flac"
    with pytest.raises(requests.exceptions.SSLError):
        uu.download_file("https://badcert/track.flac", str(dest), skip_if_exists=False)

    # No retry / no second attempt with TLS disabled.
    assert len(session.calls) == 1
    assert all(c["kwargs"].get("verify", True) is not False for c in session.calls)
    assert not dest.exists()


def test_sync_download_cleans_partial_file_on_read_error(monkeypatch, tmp_path):
    resp = _FakeSyncResponse(iter_exc=requests.exceptions.ConnectionError("read timed out"))
    session = _FakeSyncSession(response=resp)
    monkeypatch.setattr(uu, "r_session", session)

    dest = tmp_path / "track.flac"
    with pytest.raises(requests.exceptions.ConnectionError):
        uu.download_file("https://example/track.flac", str(dest), skip_if_exists=False)

    assert not dest.exists(), "partial file must be removed on failure"
    assert resp.closed is True


# ---------------------------------------------------------------------------
# Fakes for the asynchronous aiohttp path
# ---------------------------------------------------------------------------

class _FakeContent:
    def __init__(self, data):
        self._data = data

    async def iter_chunked(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i:i + n]


class _FakeAsyncResponse:
    def __init__(self, data=b"ASYNCDATA", headers=None):
        self._data = data
        self.headers = headers or {"content-length": str(len(data))}
        self.content = _FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None


class _FakeAsyncSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        return self._response


# ---------------------------------------------------------------------------
# 2b. Asynchronous route: TLS on (no ssl=False) + correct bytes
# ---------------------------------------------------------------------------

def test_async_download_uses_tls_and_writes_bytes(tmp_path):
    resp = _FakeAsyncResponse(data=b"async-flac-bytes")
    session = _FakeAsyncSession(resp)
    dest = tmp_path / "track_async.flac"

    location, downloaded = asyncio.run(
        uu.download_file_async(session, "https://example/a.flac", str(dest), skip_if_exists=False)
    )

    assert location == str(dest)
    assert downloaded == len(b"async-flac-bytes")
    assert dest.read_bytes() == b"async-flac-bytes"

    kwargs = session.calls[0]["kwargs"]
    assert kwargs.get("ssl", True) is not False, "TLS validation must not be disabled"
    assert "ssl" not in kwargs, "ssl=False was removed; default (enabled) is used"


# ---------------------------------------------------------------------------
# 3. Source guard: neither TLS-disabling flag remains in the module
# ---------------------------------------------------------------------------

def test_no_tls_disabling_flags_left_in_source():
    src = open(os.path.join(project_root, "utils", "utils.py"), encoding="utf-8").read()
    assert "verify=False" not in src, "verify=False must not appear in utils.py"
    assert "ssl=False" not in src, "ssl=False must not appear in utils.py"
