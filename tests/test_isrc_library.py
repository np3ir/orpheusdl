"""Offline regression tests: actual tagged audio, isolated SQLite and processes."""
import asyncio
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
import wave

from mutagen.id3 import TSRC
from mutagen.wave import WAVE
import pytest

from orpheus.audio_evidence import inspect_audio
from orpheus.isrc_library_index import IsrcLibraryIndex
from orpheus.library_dedup import track_guard_async, decision, register_completed
from orpheus.music_downloader import Downloader
from utils.models import CodecEnum
import isrc_dedupe as cleanup


def audio(path, isrc='TEST00000001', bits=16, rate=8000, duration=3, channels=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(b'\0' * (int(duration * rate) * channels * bits // 8))
    mf = WAVE(path)
    mf.add_tags()
    mf.tags.add(TSRC(encoding=3, text=[isrc]))
    mf.save()
    return str(path)


@pytest.fixture
def idx(tmp_path, monkeypatch):
    # Tests must not depend on a developer's private/live settings.json.
    settings = tmp_path / 'test-settings.json'
    settings.write_text('{"global":{"codecs":{"flac_only":true}}}', encoding='utf-8')
    monkeypatch.setattr('utils.audio_policy.SETTINGS_PATH', settings)
    root = tmp_path / 'library'
    root.mkdir()
    index = IsrcLibraryIndex(str(root), str(tmp_path / 'index'))
    yield index
    index.close()


def info(isrc='TEST00000001', bits=16, rate=8, duration=3):
    return SimpleNamespace(tags=SimpleNamespace(isrc=isrc), codec=CodecEnum.FLAC,
                           bit_depth=bits, sample_rate=rate, bitrate=0, duration=duration, id='track')


def downloader(idx):
    dl = object.__new__(Downloader)
    dl.path = idx.root
    dl.service_name = None
    dl.global_settings = {'general': {'isrc_library_dedup': True}}
    dl.print = lambda *a, **k: None
    dl._config_dir = lambda: str(Path(idx.db_path).parent)
    return dl


def test_shared_index_sees_new_files_and_verifies_changed_tags(idx):
    other = IsrcLibraryIndex(idx.root, str(Path(idx.db_path).parent))
    try:
        assert other.find('TEST00000001') is None
        p = audio(Path(idx.root) / 'a.wav')
        assert idx.add('TEST00000001', p)
        assert other.find('TEST00000001') == p
        mf = WAVE(p); mf.tags.setall('TSRC', [TSRC(encoding=3, text=['DIFFERENT'])]); mf.save()
        assert other.find('TEST00000001') is None
    finally:
        other.close()


def test_missing_invalid_wrong_isrc_and_outside_never_registered(idx, tmp_path):
    invalid = Path(idx.root) / 'invalid.flac'; invalid.write_bytes(b'not audio')
    assert not idx.add('TEST00000001', str(invalid))
    assert not idx.add('TEST00000001', str(invalid.with_name('missing.wav')))
    p = audio(Path(idx.root) / 'wrong.wav', isrc='OTHER')
    assert not idx.add('TEST00000001', p)
    p = audio(tmp_path / 'outside.wav')
    assert not idx.add('TEST00000001', p)
    assert idx.row_count() == 0


def test_remaining_copy_found_and_best_quality_ranked(idx):
    a = audio(Path(idx.root) / 'a.wav', bits=16)
    b = audio(Path(idx.root) / 'b.wav', bits=24)
    idx.add('TEST00000001', a); idx.add('TEST00000001', b)
    assert idx.find('TEST00000001') == b
    Path(b).unlink()
    assert idx.find('TEST00000001') == a


def test_roots_are_independent(idx, tmp_path):
    p = audio(Path(idx.root) / 'a.wav'); idx.add('TEST00000001', p)
    other_root = tmp_path / 'external'; other_root.mkdir()
    other = IsrcLibraryIndex(str(other_root), str(Path(idx.db_path).parent))
    try:
        assert idx.db_path != other.db_path
        assert other.find('TEST00000001') is None
    finally:
        other.close()


def test_no_implicit_scan(idx, monkeypatch):
    monkeypatch.setattr(idx, 'build', lambda **k: pytest.fail('implicit scan'))
    idx.ensure_fresh()
    idx.duplicates()


def test_build_skips_quarantine_and_aborts_prune_on_walk_error(idx, monkeypatch):
    a = audio(Path(idx.root) / 'a.wav')
    audio(Path(idx.root) / '_duplicados' / 'b.wav')
    result = idx.build(workers=2)
    assert result['total_files'] == 1
    previous = idx.last_scan()
    def broken_walk():
        idx._scan_error(OSError('NAS disconnected'))
        yield from ()
    monkeypatch.setattr(idx, '_iter_audio', broken_walk)
    assert idx.build()['error'] == 'incomplete_scan'
    assert idx.last_scan() == previous
    assert idx.find('TEST00000001') == a


def test_quality_upgrade_is_additive_and_duration_is_checked(idx):
    a = audio(Path(idx.root) / 'a.wav'); idx.add('TEST00000001', a)
    match, target = decision(idx, info(bits=24), a, upgrade=True)
    assert match is None and target != a and Path(a).exists()
    assert decision(idx, info(bits=24), a, upgrade=False)[0] == a
    assert decision(idx, info(duration=30), a)[0] is None


def _competing_writer(root, dbdir, output, start, results):
    index = IsrcLibraryIndex(root, dbdir)
    start.wait(10)
    try:
        with index.claim('TEST00000001', timeout=10):
            if index.find('TEST00000001'):
                results.put('skip')
            else:
                path = audio(output)
                assert index.add('TEST00000001', path)
                results.put('written')
    finally:
        index.close()


def test_two_processes_publish_one_recording(idx):
    ctx = multiprocessing.get_context('spawn')
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_competing_writer, args=(idx.root, str(Path(idx.db_path).parent),
                 str(Path(idx.root) / (name + '.wav')), start, results)) for name in ('a', 'b')]
    for p in processes: p.start()
    start.set()
    for p in processes:
        p.join(20)
        if p.is_alive(): p.terminate(); p.join(); pytest.fail('lock deadlock')
        assert p.exitcode == 0
    assert sorted(results.get(timeout=2) for _ in processes) == ['skip', 'written']
    assert len(list(Path(idx.root).glob('*.wav'))) == 1
    results.close()


def test_async_cancel_releases_lock_and_wait_does_not_block_loop(idx):
    idx.build(); dl = downloader(idx)
    async def scenario():
        entered = asyncio.Event()
        async def owner():
            async with track_guard_async(dl, info()):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(owner())
        await entered.wait()
        async def waiter():
            async with track_guard_async(dl, info()):
                return True
        waiting = asyncio.create_task(waiter())
        await asyncio.sleep(.15)
        assert not waiting.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert await asyncio.wait_for(waiting, 3)
    asyncio.run(scenario())


def test_async_downloader_uses_library_and_correct_playlist_path(idx):
    a = audio(Path(idx.root) / 'other_album' / 'a.wav')
    idx.build(); dl = downloader(idx)
    dl._require_native_flac = lambda *a: None
    dl._apply_track_index_to_tags = lambda *a: None
    dl._skip_existing_files_enabled = lambda: True
    dl._create_track_location = lambda *a, **k: str(Path(idx.root) / 'new.wav')
    dl._resolve_track_filename_conflict = lambda *a: pytest.fail('should skip before path dedup')
    playlist = []
    dl._add_track_m3u_playlist = lambda name, track, path: playlist.append(path)
    result = asyncio.run(dl._download_track_async(None, track_info=info(),
             download_info=SimpleNamespace(different_codec=None), m3u_playlist='list.m3u'))
    assert result == 'ALREADY_EXISTS'
    assert playlist == [a]


def plan_pair(idx):
    a = audio(Path(idx.root) / 'a.wav', bits=16)
    b = audio(Path(idx.root) / 'b.wav', bits=24)
    idx.build()
    plan = cleanup.make_plan(idx, workers=2, progress=lambda *a: None)
    return a, b, plan


def test_plan_apply_resume_and_rollback(idx, tmp_path):
    a, b, plan = plan_pair(idx)
    assert plan['groups'][0]['keep']['path'] == b
    journal = tmp_path / 'moves.jsonl'
    assert cleanup.apply_plan(idx, plan, journal)['moved'] == 1
    assert not Path(a).exists() and Path(b).exists()
    assert cleanup.apply_plan(idx, plan, journal)['already_moved'] == 1
    assert cleanup.rollback(idx, journal)['restored'] == 1
    assert Path(a).exists() and Path(b).exists()
    assert cleanup.rollback(idx, journal)['already_restored'] == 1
    with pytest.raises(ValueError, match='rolled back'):
        cleanup.apply_plan(idx, plan, journal)


def test_changed_keeper_blocks_move(idx, tmp_path):
    a, b, plan = plan_pair(idx)
    Path(b).write_bytes(b'corrupt')
    assert cleanup.apply_plan(idx, plan, tmp_path / 'j.jsonl')['errors'] == 1
    assert Path(a).exists()


def test_wrong_isrc_or_duration_groups_go_to_review(idx):
    a = audio(Path(idx.root) / 'a.wav', duration=3)
    b = audio(Path(idx.root) / 'b.wav', duration=10)
    idx.build()
    plan = cleanup.make_plan(idx, progress=lambda *a: None)
    assert not plan['groups'] and len(plan['review']) == 1


def test_crash_after_rename_recovered_from_intent(idx, tmp_path, monkeypatch):
    a, b, plan = plan_pair(idx)
    journal = tmp_path / 'j.jsonl'
    original = cleanup.journal_event
    def crash(handle, **event):
        if event['event'] == 'moved': raise RuntimeError('simulated power loss')
        original(handle, **event)
    monkeypatch.setattr(cleanup, 'journal_event', crash)
    with pytest.raises(RuntimeError): cleanup.apply_plan(idx, plan, journal)
    assert not Path(a).exists()
    monkeypatch.setattr(cleanup, 'journal_event', original)
    assert cleanup.rollback(idx, journal)['restored'] == 1
    assert Path(a).exists()


def test_torn_journal_tail_is_archived(idx, tmp_path):
    a, b, plan = plan_pair(idx)
    journal = tmp_path / 'j.jsonl'
    cleanup.apply_plan(idx, plan, journal)
    with open(journal, 'ab') as handle: handle.write(b'{"event":"mov\xc3')
    assert cleanup.rollback(idx, journal)['restored'] == 1
    assert list(tmp_path.glob('*.torn-*'))
    assert cleanup.read_journal(journal)[-1]['event'] == 'restored'


def test_plan_cannot_escape_root_or_move_keeper(idx, tmp_path):
    a, b, plan = plan_pair(idx)
    plan['groups'][0]['move'][0]['path'] = str(tmp_path / 'outside.wav')
    with pytest.raises(ValueError): cleanup.apply_plan(idx, plan, tmp_path / 'j.jsonl')
    assert Path(a).exists() and Path(b).exists()


def test_rollback_never_overwrites_new_file(idx, tmp_path):
    a, b, plan = plan_pair(idx)
    journal = tmp_path / 'j.jsonl'
    cleanup.apply_plan(idx, plan, journal)
    Path(a).write_bytes(b'new data')
    assert cleanup.rollback(idx, journal)['errors'] == 1
    assert Path(a).read_bytes() == b'new data'


def test_async_dedup_precedes_service_audio_factory(idx):
    audio(Path(idx.root) / 'a.wav'); idx.build(); dl = downloader(idx)
    dl._require_native_flac = lambda *a: None
    dl._apply_track_index_to_tags = lambda *a: None
    dl._skip_existing_files_enabled = lambda: True
    dl._create_track_location = lambda *a, **k: str(Path(idx.root) / 'new.wav')
    result = asyncio.run(dl._download_track_async(None, track_info=info(),
        download_info_factory=lambda: pytest.fail('must not transfer duplicate audio')))
    assert result == 'ALREADY_EXISTS'


def test_sync_downloader_dedup_precedes_service_transfer(idx):
    from utils.models import TrackInfo, Tags
    p = audio(Path(idx.root) / 'a.wav'); idx.build(); dl = downloader(idx)
    track = TrackInfo(name='Song', album='Album', album_id='album', artists=['Artist'],
                      tags=Tags(isrc='TEST00000001'), codec=CodecEnum.FLAC,
                      cover_url='', release_year=2020, duration=3, sample_rate=8)
    dl.global_settings.update(codecs={'spatial_codecs':False,'proprietary_codecs':False}, formatting={})
    dl.global_settings['general']['download_quality']='LOSSLESS'
    dl.set_indent_number=lambda *a: None
    dl.oprinter=SimpleNamespace(oprint=lambda *a, **k: None)
    dl._get_status_symbols=lambda: {'error':'X','skip':'S','success':'O'}
    dl._ensure_can_download_or_abort=lambda *a: True
    dl._filter_kwargs_for_method=lambda *a: {}
    dl._ensure_track_info_id=lambda track, id: track
    dl._apply_album_context_to_track=lambda *a: None
    dl._apply_track_index_to_tags=lambda *a: None
    dl._create_track_location=lambda *a, **k: str(Path(idx.root)/'new.wav')
    dl.service=SimpleNamespace(get_track_info=lambda *a, **k: track,
        get_track_download=lambda *a, **k: pytest.fail('duplicate must not transfer'))
    dl.track_skipped_count=0
    dl._add_track_m3u_playlist=lambda *args: None
    assert dl.download_track('track', album_location=idx.root, verbose=False) == 'SKIPPED'
    assert dl.track_skipped_count==1


def test_async_transfer_registered_only_after_final_conversion(idx, tmp_path):
    from utils.models import DownloadEnum
    idx.build(); dl = downloader(idx)
    track=info(rate=44.1); track.cover_url=''
    dl.global_settings.update(covers={'embed_cover':False,'save_external':False}, formatting={},
                              lyrics={'save_synced_lyrics':False})
    dl._require_native_flac=lambda *a: None
    dl._apply_track_index_to_tags=lambda *a: None
    dl._skip_existing_files_enabled=lambda: True
    dest=str(Path(idx.root)/'dest.wav'); final=str(Path(idx.root)/'converted.wav')
    dl._create_track_location=lambda *a, **k: dest
    dl._resolve_track_filename_conflict=lambda path, info: path
    dl._prepare_track_download_path=lambda *a: None
    def convert(path, *_):
        assert idx.find('TEST00000001') is None
        os.rename(path,final)
        return final,None,None
    dl._convert_file_if_needed=convert
    dl._fetch_metadata=lambda *a: None
    def factory():
        assert idx.row_count()==0
        temp=audio(tmp_path/'service-temp.wav',rate=44100)
        return SimpleNamespace(download_type=DownloadEnum.TEMP_FILE_PATH,temp_file_path=temp,different_codec=None)
    result=asyncio.run(dl._download_track_async(None,track_info=track,download_info_factory=factory))
    assert isinstance(result,tuple) and result[0]==final
    assert idx.find('TEST00000001')==final
    assert not Path(dest).exists()


def test_cancelled_io_finishes_before_unwinding():
    import threading
    from orpheus.library_dedup import finish_io
    started=threading.Event(); release=threading.Event(); finished=threading.Event()
    def worker():
        started.set(); release.wait(5); finished.set()
    async def scenario():
        task=asyncio.create_task(finish_io(asyncio.to_thread(worker)))
        await asyncio.to_thread(started.wait,2)
        task.cancel()
        await asyncio.sleep(.05)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert finished.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize('fail_tagging', [False, True])
def test_async_registration_follows_successful_tagging(idx, tmp_path, monkeypatch, fail_tagging):
    from utils.models import DownloadEnum
    idx.build(); dl=downloader(idx)
    track=info(rate=44.1); track.cover_url=''
    dl.global_settings.update(covers={'embed_cover':False,'save_external':False}, formatting={},
                              lyrics={'save_synced_lyrics':False})
    dl._require_native_flac=lambda *a: None
    dl._apply_track_index_to_tags=lambda *a: None
    dl._skip_existing_files_enabled=lambda: True
    dest=str(Path(idx.root)/'dest.flac')
    dl._create_track_location=lambda *a, **k: dest
    dl._resolve_track_filename_conflict=lambda path, info: path
    dl._prepare_track_download_path=lambda *a: None
    dl._convert_file_if_needed=lambda path,*a: (path,None,None)
    dl._fetch_metadata=lambda *a: None
    dl._service_display_name=lambda: 'test'
    called=[]
    def tag(*args, **kwargs):
        called.append(True)
        assert idx.row_count()==0
        if fail_tagging: raise ValueError('simulated tagging failure')
    monkeypatch.setattr('orpheus.tagging.tag_file',tag)
    # Exercise the tagged-container lifecycle with our actual PCM/WAVE fixture.
    # Select its parser explicitly because the destination extension is FLAC.
    monkeypatch.setattr('orpheus.audio_evidence.mutagen.File', lambda path: WAVE(path))
    def factory():
        return SimpleNamespace(download_type=DownloadEnum.TEMP_FILE_PATH,
            temp_file_path=audio(tmp_path/'temp.wav',rate=44100),different_codec=None)
    result=asyncio.run(dl._download_track_async(None,track_info=track,download_info_factory=factory))
    assert called==[True]
    if fail_tagging:
        assert result is None and idx.row_count()==0
    else:
        assert result[0]==dest and idx.find('TEST00000001')==dest


def test_scan_preserves_prior_evidence_on_read_failure(idx):
    path=audio(Path(idx.root)/'a.wav'); idx.build()
    previous=idx.last_scan()
    Path(path).write_bytes(b'unreadable')
    assert idx.build()['error']=='incomplete_scan'
    assert idx.last_scan()==previous
    assert idx.row_count()==1
    assert idx.find('TEST00000001') is None
    audio(path)
    assert 'error' not in idx.build()
    assert idx.find('TEST00000001')==path
