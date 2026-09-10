"""Offline tests for session transactions and atomic transfers."""
import asyncio
import multiprocessing
from pathlib import Path
import pickle
import time
from types import SimpleNamespace

import aiohttp
import pytest
import requests

from utils import atomic_io as atomic
from utils import utils as uu
from test_delivery1_immediate_fixes import (
    _FakeSyncResponse, _FakeSyncSession, _FakeAsyncResponse, _FakeAsyncSession,
)


def _write_sessions(path, key, start):
    start.wait(10)
    for index in range(12):
        uu.set_temporary_setting(path, 'demo', 'custom_data', key, index)


def _hold_lock(path, ready, release):
    with atomic.file_lock(path):
        ready.set()
        release.wait(10)


def _publish(path, temp, start):
    start.wait(10)
    try:
        atomic.publish_download(temp, path, True)
    finally:
        atomic.remove_temporary(temp)


def _join(processes):
    for process in processes:
        process.join(15)
        assert process.exitcode == 0


def test_sessions_two_processes_keep_both_updates(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    start = ctx.Event()
    path = str(tmp_path / 'sessions.bin')
    processes = [ctx.Process(target=_write_sessions, args=(path, key, start)) for key in ('a', 'b')]
    for process in processes:
        process.start()
    start.set()
    _join(processes)
    assert uu.read_temporary_setting(path, 'demo', 'custom_data') == {'a': 11, 'b': 11}
    uu.remove_module_from_storage(path, 'demo')
    assert atomic.read_pickle(path)['modules'] == {}


def test_lock_timeout_and_release(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    path = str(tmp_path / 'sessions.bin')
    child = ctx.Process(target=_hold_lock, args=(path, ready, release))
    child.start()
    try:
        assert ready.wait(10)
        with pytest.raises(TimeoutError):
            with atomic.file_lock(path, timeout=0.15):
                pytest.fail('lock unexpectedly acquired')
    finally:
        release.set()
        child.join(15)
    assert child.exitcode == 0
    with atomic.file_lock(path, timeout=1):
        pass


def test_failed_session_commit_preserves_original(monkeypatch, tmp_path):
    path = tmp_path / 'sessions.bin'
    original = pickle.dumps({'modules': {'keep': {}}})
    path.write_bytes(original)
    def fail(*args):
        raise OSError('simulated publication failure')
    monkeypatch.setattr(atomic.os, 'replace', fail)
    with pytest.raises(OSError):
        with atomic.session_transaction(path) as state:
            state['modules']['new'] = {}
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_corrupt_session_is_never_reset(tmp_path):
    path = tmp_path / 'sessions.bin'
    path.write_bytes(b'not a pickle')
    with pytest.raises(pickle.UnpicklingError):
        uu.set_temporary_setting(path, 'demo', 'custom_data', 'key', 'value')
    assert path.read_bytes() == b'not a pickle'


def test_core_and_utils_preserve_session_fields(tmp_path):
    from orpheus.core import Orpheus
    from utils.models import ModuleFlags
    path = tmp_path / 'sessions.bin'
    with atomic.session_transaction(path) as state:
        state['advancedmode'] = True
    uu.set_temporary_setting(path, 'demo', 'custom_data', 'key', 'preserved')
    core = object.__new__(Orpheus)
    core.settings = {'global': {'advanced': {'advanced_login_system': True}}, 'modules': {}}
    core.default_global_settings = {'advanced': {'advanced_login_system': True}}
    core.extension_list = []
    core.module_list = ['demo']
    core.module_settings = {'demo': SimpleNamespace(
        global_settings={}, session_settings={}, global_storage_variables=[],
        session_storage_variables=['key'], login_behaviour=None, flags=ModuleFlags(0))}
    core.session_storage_location = str(path)
    core.settings_location = str(tmp_path / 'settings.json')
    core.update_module_storage()
    assert uu.read_temporary_setting(path, 'demo', 'custom_data', 'key') == 'preserved'


def test_processes_publish_one_complete_file(tmp_path):
    path = tmp_path / 'track.flac'
    ctx = multiprocessing.get_context('spawn')
    start = ctx.Event()
    temps = [atomic.temporary_sibling(path) for _ in range(2)]
    for temporary, data in zip(temps, (b'A'*10000, b'B'*10000)):
        Path(temporary).write_bytes(data)
    processes = [ctx.Process(target=_publish, args=(str(path), temporary, start)) for temporary in temps]
    for process in processes:
        process.start()
    start.set()
    _join(processes)
    assert path.read_bytes() in (b'A'*10000, b'B'*10000)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('asynchronous', [False, True])
def test_commit_failure_preserves_download(monkeypatch, tmp_path, asynchronous):
    path = tmp_path / 'track.flac'
    path.write_bytes(b'original')
    def fail(*args):
        raise OSError('simulated failure')
    monkeypatch.setattr(atomic.os, 'replace', fail)
    monkeypatch.setattr(uu, 'r_session', _FakeSyncSession(_FakeSyncResponse()))
    with pytest.raises(OSError):
        if asynchronous:
            asyncio.run(uu.download_file_async(_FakeAsyncSession(_FakeAsyncResponse()),
                                               'https://example', str(path), skip_if_exists=False))
        else:
            uu.download_file('https://example', str(path), skip_if_exists=False)
    assert path.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [path]


def test_sync_read_failure_preserves_existing_destination(monkeypatch, tmp_path):
    path = tmp_path / 'track.flac'
    path.write_bytes(b'original')
    class Partial(_FakeSyncResponse):
        def iter_content(self, chunk_size):
            assert path.read_bytes() == b'original'
            yield b'partial'
            raise requests.exceptions.ConnectionError('disconnected')
    monkeypatch.setattr(uu, 'r_session', _FakeSyncSession(Partial()))
    with pytest.raises(requests.exceptions.ConnectionError):
        uu.download_file('https://example', str(path), skip_if_exists=False)
    assert path.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [path]


def test_async_retry_resets_byte_count_and_file(monkeypatch, tmp_path):
    path = tmp_path / 'track.flac'
    class BrokenContent:
        async def iter_chunked(self, size):
            yield b'partial-failed-attempt'
            raise aiohttp.ClientPayloadError('disconnected')
    first = _FakeAsyncResponse()
    first.content = BrokenContent()
    second = _FakeAsyncResponse(b'complete')
    class Session:
        def __init__(self):
            self.responses = iter([first, second])
        def get(self, *args, **kwargs):
            assert not path.exists()
            return next(self.responses)
    async def no_delay(*args):
        pass
    monkeypatch.setattr(uu.asyncio, 'sleep', no_delay)
    result = asyncio.run(uu.download_file_async(Session(), 'https://example', str(path)))
    assert result == (str(path), len(b'complete'))
    assert path.read_bytes() == b'complete'
    assert list(tmp_path.iterdir()) == [path]


def test_async_cancellation_cleans_temporary_and_preserves_destination(tmp_path):
    path = tmp_path / 'track.flac'
    path.write_bytes(b'original')
    async def scenario():
        written = asyncio.Event()
        class Content:
            async def iter_chunked(self, size):
                yield b'partial'
                written.set()
                await asyncio.Event().wait()
        response = _FakeAsyncResponse()
        response.content = Content()
        task = asyncio.create_task(uu.download_file_async(_FakeAsyncSession(response),
                                  'https://example', str(path), skip_if_exists=False))
        await asyncio.wait_for(written.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert path.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('asynchronous', [False, True])
def test_short_identity_response_not_published(monkeypatch, tmp_path, asynchronous):
    path = tmp_path / 'track.flac'
    headers = {'content-length': '999'}
    if asynchronous:
        with pytest.raises(aiohttp.ClientPayloadError):
            asyncio.run(uu.download_file_async(_FakeAsyncSession(_FakeAsyncResponse(b'short', headers)),
                       'https://example', str(path), max_retries=1))
    else:
        monkeypatch.setattr(uu, 'r_session', _FakeSyncSession(_FakeSyncResponse(b'short', headers)))
        with pytest.raises(requests.exceptions.ChunkedEncodingError):
            uu.download_file('https://example', str(path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('asynchronous', [False, True])
def test_compressed_content_length_is_not_compared_to_decoded_size(monkeypatch, tmp_path, asynchronous):
    path = tmp_path / 'track.flac'
    headers = {'content-length': '3', 'content-encoding': 'gzip'}
    data = b'already decompressed content'
    if asynchronous:
        asyncio.run(uu.download_file_async(_FakeAsyncSession(_FakeAsyncResponse(data, headers)),
                    'https://example', str(path)))
    else:
        monkeypatch.setattr(uu, 'r_session', _FakeSyncSession(_FakeSyncResponse(data, headers)))
        uu.download_file('https://example', str(path))
    assert path.read_bytes() == data


def test_async_certificate_error_is_not_retried(tmp_path):
    class Session:
        calls = 0
        def get(self, *args, **kwargs):
            self.calls += 1
            raise aiohttp.ClientSSLError(None, OSError('certificate failure'))
    session = Session()
    with pytest.raises(aiohttp.ClientSSLError):
        asyncio.run(uu.download_file_async(session, 'https://example', str(tmp_path/'track.flac')))
    assert session.calls == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('asynchronous', [False, True])
def test_concurrent_downloads_recheck_destination(monkeypatch, tmp_path, asynchronous):
    path = tmp_path / 'track.flac'
    if asynchronous:
        async def scenario():
            ready = asyncio.Event()
            count = 0
            class Content:
                async def iter_chunked(self, size):
                    nonlocal count
                    count += 1
                    if count == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), 2)
                    yield b'complete'
            response = _FakeAsyncResponse(b'complete')
            response.content = Content()
            session = _FakeAsyncSession(response)
            return await asyncio.gather(*[
                uu.download_file_async(session, 'https://example', str(path)) for _ in range(2)])
        results = asyncio.run(scenario())
        assert sorted(result[1] for result in results) == [0, 8]
    else:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        ready = threading.Barrier(2, timeout=3)
        class Response(_FakeSyncResponse):
            def iter_content(self, chunk_size):
                ready.wait()
                yield b'complete'
        class Session:
            def get(self, *args, **kwargs):
                return Response(b'complete')
        monkeypatch.setattr(uu, 'r_session', Session())
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(uu.download_file, 'https://example', str(path)) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]
        assert results.count(None) == 1
        assert results.count(str(path)) == 1
    assert path.read_bytes() == b'complete'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('asynchronous', [False, True])
def test_artwork_transform_finishes_before_publication(monkeypatch, tmp_path, asynchronous):
    from io import BytesIO
    from PIL import Image
    buffer = BytesIO()
    Image.new('RGB', (4, 4), 'red').save(buffer, format='PNG')
    data = buffer.getvalue()
    path = tmp_path / 'cover.png'
    settings = {'should_resize': True, 'resolution': 2, 'format': 'png'}
    if asynchronous:
        asyncio.run(uu.download_file_async(_FakeAsyncSession(_FakeAsyncResponse(data)),
                    'https://example', str(path), artwork_settings=settings))
    else:
        monkeypatch.setattr(uu, 'r_session', _FakeSyncSession(_FakeSyncResponse(data)))
        uu.download_file('https://example', str(path), artwork_settings=settings)
    with Image.open(path) as result:
        assert result.size == (2, 2)
    assert list(tmp_path.iterdir()) == [path]
