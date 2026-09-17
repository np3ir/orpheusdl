"""Offline regression tests for the local native-FLAC-only policy."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from utils.models import CodecEnum, DownloadEnum, TrackDownloadInfo
ROOT = Path(__file__).resolve().parents[1]

def method(file, name, **extra):
    tree = ast.parse((ROOT / file).read_text(encoding='utf-8-sig'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    env = dict(CodecEnum=CodecEnum, DownloadEnum=DownloadEnum, TrackDownloadInfo=TrackDownloadInfo, flac_only=lambda: True)
    env.update(extra)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])), file, 'exec'), env)
    return env[name]

@pytest.mark.parametrize('codec', [CodecEnum.MP3, CodecEnum.AAC, CodecEnum.ALAC, CodecEnum.MQA, None])
def test_common_rejects_non_native_flac(codec):
    check = method('orpheus/music_downloader.py', '_require_native_flac')
    with pytest.raises(ValueError): check(SimpleNamespace(codec=codec))
    check(SimpleNamespace(codec=CodecEnum.FLAC))

@pytest.mark.parametrize('codec', [CodecEnum.MP3, CodecEnum.AAC, None])
def test_tidal_rejects_before_network(codec):
    get = method('modules/tidal/interface.py', 'get_track_download', AudioTrack=object)
    with pytest.raises(ValueError): get(SimpleNamespace(), file_url='https://invalid.test/audio', codec=codec)
    result = get(SimpleNamespace(), file_url='https://invalid.test/audio', codec=CodecEnum.FLAC)
    assert result.download_type == DownloadEnum.URL

def test_tidal_dash_rejected_before_segment_download():
    get = method('modules/tidal/interface.py', 'get_track_download', AudioTrack=object)
    with pytest.raises(ValueError): get(SimpleNamespace(), audio_track=SimpleNamespace(codec=CodecEnum.AAC))

@pytest.mark.parametrize('fmt', ['MP3_320','MP3_128','MP3_MISC',None])
def test_deezer_rejects_before_auth_or_transfer(fmt):
    get = method('modules/deezer/interface.py', 'get_track_download')
    with pytest.raises(ValueError): get(SimpleNamespace(), '1','token',0,fmt)

def test_qobuz_direct_url_requires_flac_evidence():
    get = method('modules/qobuz/interface.py', 'get_track_download')
    for fmt in (None,5,'5'):
        with pytest.raises(ValueError): get(SimpleNamespace(), 'https://invalid.test/audio', format_id=fmt)
    for fmt in (6,7,27,'27'):
        assert get(SimpleNamespace(), 'https://invalid.test/audio', format_id=fmt).download_type == DownloadEnum.URL

def test_tidal_api_fallback_never_requests_lossy():
    class RequestError(Exception): pass
    get = method('modules/tidal/tidal_api.py','get_stream_url',TidalRequestError=RequestError,TidalError=RequestError)
    requests=[]
    def fetch(endpoint, params):
        requests.append(params.copy())
        if len(requests)==1: raise RequestError()
        return {'ok':True}
    obj=SimpleNamespace(_get=fetch)
    assert get(obj,'1','HI_RES_LOSSLESS') == {'ok':True}
    assert [p['audioquality'] for p in requests] == ['HI_RES_LOSSLESS','LOSSLESS']
    with pytest.raises(ValueError): get(obj,'1','HIGH')
    assert len(requests)==2

@pytest.mark.parametrize('fmt', [5,'5',None])
def test_qobuz_api_blocks_mp3_without_request(fmt):
    get=method('modules/qobuz/qobuz_api.py','get_file_url')
    with pytest.raises(ValueError): get(SimpleNamespace(),'1',fmt)

def test_conversion_disabled_even_if_ui_changes_settings():
    convert=method('orpheus/music_downloader.py','_convert_file_if_needed')
    assert convert(SimpleNamespace(), 'track.flac', SimpleNamespace(codec=CodecEnum.FLAC), print)==('track.flac',None,None)

@pytest.mark.parametrize('name', ['_get_track_download_librespot','_get_episode_download_pathfinder','_get_episode_download_librespot'])
def test_spotify_lossy_transfers_are_disabled(name):
    get=method('modules/spotify/spotify_api.py',name)
    with pytest.raises(ValueError): get(SimpleNamespace())

def test_disabled_common_allows_other_codecs():
    check=method('orpheus/music_downloader.py','_require_native_flac',flac_only=lambda:False)
    check(SimpleNamespace(codec=CodecEnum.AAC))

def test_disabled_tidal_allows_aac_url():
    get=method('modules/tidal/interface.py','get_track_download',flac_only=lambda:False)
    assert get(SimpleNamespace(),file_url='https://invalid.test/audio',codec=CodecEnum.AAC).download_type==DownloadEnum.URL

def test_disabled_tidal_api_preserves_requested_quality():
    fetch=Mock(return_value={'ok':True})
    get=method('modules/tidal/tidal_api.py','get_stream_url',flac_only=lambda:False)
    assert get(SimpleNamespace(_get=fetch),'1','HIGH')=={'ok':True}
    assert fetch.call_args.args[1]['audioquality']=='HIGH'

def test_disabled_deezer_allows_normal_mp3_route():
    session=SimpleNamespace(get_track_url=Mock(return_value='url'),dl_track=Mock())
    get=method('modules/deezer/interface.py','get_track_download',flac_only=lambda:False,create_temp_filename=lambda:'temp')
    result=get(SimpleNamespace(session=session,_ensure_credentials=lambda:None),'1','token',0,'MP3_320')
    session.dl_track.assert_called_once_with('1','url','temp')
    assert result.temp_file_path=='temp'

def test_disabled_qobuz_allows_normal_url():
    get=method('modules/qobuz/interface.py','get_track_download',flac_only=lambda:False)
    assert get(SimpleNamespace(),'https://invalid.test/audio',format_id=5).download_type==DownloadEnum.URL

def test_setting_reads_both_modes_and_rejects_strings(tmp_path,monkeypatch):
    from utils import audio_policy
    config=tmp_path/'settings.json'
    monkeypatch.setattr(audio_policy,'SETTINGS_PATH',config)
    try:
        for literal,expected in [('true',True),('false',False)]:
            config.write_text('{"global":{"codecs":{"flac_only":'+literal+'}}}')
            audio_policy.flac_only.cache_clear()
            assert audio_policy.flac_only() is expected
        config.write_text('{"global":{"codecs":{"flac_only":"false"}}}')
        audio_policy.flac_only.cache_clear()
        with pytest.raises(ValueError):audio_policy.flac_only()
    finally:
        audio_policy.flac_only.cache_clear()
