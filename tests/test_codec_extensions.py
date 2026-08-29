"""Unit tests for the codec-to-extension mapping in ``Downloader._create_track_location``.

Background: the file extension a downloaded track gets is chosen from
``CODEC_EXTENSIONS`` (previously a local dict inside ``_create_track_location``),
keyed by the resolved codec (``override_codec`` when the module remuxes into a
different container, e.g. Tidal Atmos -> M4A, otherwise ``track_info.codec``).
Codecs missing from the map fall back to ``DEFAULT_TRACK_EXTENSION`` ('.flac').

The regression this guards: Sony 360RA (MHM1) tracks were downloaded as '.flac'
because the two MPEG-H 3D Audio codecs (MHA1/MHM1) were missing from the map.
MHM1 is now remuxed into a proper '.mp4' container, MHA1 stays '.m4a'.
"""

import os
import sys

import pytest

# Make the project root importable when this file is run directly
# (e.g. ``python tests/test_codec_extensions.py``) rather than via pytest.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from orpheus.music_downloader import (  # noqa: E402
    CODEC_EXTENSIONS,
    DEFAULT_TRACK_EXTENSION,
    Downloader,
)
from utils.models import CodecEnum, Tags, TrackInfo  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The mapping itself
# ---------------------------------------------------------------------------

def test_all_spatial_codecs_have_an_explicit_extension():
    """No spatial codec may fall through to the '.flac' default."""
    from utils.models import codec_data

    spatial_codecs = [
        codec for codec in CodecEnum
        if codec != CodecEnum.NONE and codec_data[codec].spatial
    ]
    assert spatial_codecs, "expected at least one spatial codec in codec_data"
    for codec in spatial_codecs:
        assert codec in CODEC_EXTENSIONS, (
            f"spatial codec {codec.name} must have an explicit extension, "
            f"got default {DEFAULT_TRACK_EXTENSION!r}"
        )


def test_ra360_mhm1_maps_to_mp4():
    """Sony 360RA (MHM1) must be a proper .mp4, not the .flac fallback."""
    assert CODEC_EXTENSIONS[CodecEnum.MHM1] == ".mp4"


def test_ra360_mha1_maps_to_m4a():
    """Sony 360RA (MHA1) stays .m4a like the other MPEG-4 audio codecs."""
    assert CODEC_EXTENSIONS[CodecEnum.MHA1] == ".m4a"


def test_atmos_codecs_map_to_m4a():
    """Dolby Atmos (EAC3/AC4) and plain AC3 keep their established containers."""
    assert CODEC_EXTENSIONS[CodecEnum.EAC3] == ".m4a"
    assert CODEC_EXTENSIONS[CodecEnum.AC4] == ".m4a"
    assert CODEC_EXTENSIONS[CodecEnum.AC3] == ".ac3"


def test_non_spatial_codecs_keep_their_extensions():
    """The ordinary codecs must be unaffected by the spatial fixes."""
    assert CODEC_EXTENSIONS[CodecEnum.FLAC] == ".flac"
    assert CODEC_EXTENSIONS[CodecEnum.AAC] == ".m4a"
    assert CODEC_EXTENSIONS[CodecEnum.ALAC] == ".m4a"
    assert CODEC_EXTENSIONS[CodecEnum.MP3] == ".mp3"
    assert CODEC_EXTENSIONS[CodecEnum.OPUS] == ".opus"
    assert CODEC_EXTENSIONS[CodecEnum.VORBIS] == ".ogg"
    assert CODEC_EXTENSIONS[CodecEnum.WAV] == ".wav"
    assert CODEC_EXTENSIONS[CodecEnum.AIFF] == ".aiff"


def test_mqa_has_explicit_flac_extension():
    """MQA is stored in a FLAC container, so it must map explicitly — not rely on the default."""
    assert CODEC_EXTENSIONS[CodecEnum.MQA] == ".flac"


def test_heaac_maps_to_m4a_not_flac():
    """HE-AAC lives in an MP4 audio container; the old .flac fallback was wrong."""
    assert CODEC_EXTENSIONS[CodecEnum.HEAAC] == ".m4a"


def test_unknown_codec_falls_back_to_flac():
    """Codecs not listed (e.g. NONE) must keep the historical '.flac' default."""
    assert CODEC_EXTENSIONS.get(CodecEnum.NONE, DEFAULT_TRACK_EXTENSION) == ".flac"


def test_extensions_are_derived_from_codec_data_containers():
    """CODEC_EXTENSIONS must always mirror codec_data containers (except NONE)."""
    from utils.models import codec_data

    expected = {
        codec: f".{data.container.name}"
        for codec, data in codec_data.items()
        if codec != CodecEnum.NONE
    }
    assert CODEC_EXTENSIONS == expected


# ---------------------------------------------------------------------------
# 2. _create_track_location end-to-end (the extension lands on the filename)
# ---------------------------------------------------------------------------

def _minimal_downloader():
    """Create a Downloader without running its heavy __init__."""
    downloader = object.__new__(Downloader)
    downloader.path = "K:/OrpheusDL-test"
    downloader.service_name = None
    downloader.service = None
    downloader.download_mode = None
    downloader.module_settings = {}
    downloader.global_settings = {
        "formatting": {
            "single_full_path_format": "{artist} - {name}",
            "track_filename_format": "{track_number}. {name}",
            "filename_separator": ", ",
            "metadata_separator": ", ",
            "enable_zfill": False,
            "split_metadata": False,
        },
        "playlist": {},
        "advanced": {},
    }
    return downloader


def _make_track(codec):
    return TrackInfo(
        name="Test Track",
        album="Test Album",
        album_id="1",
        artists=["Test Artist"],
        tags=Tags(album_artist="Test Artist"),
        codec=codec,
        cover_url="",
        release_year=2024,
    )


def _track_filename(downloader, track, override_codec=None):
    location = downloader._create_track_location(
        downloader.path, track, override_codec=override_codec
    )
    return os.path.basename(location)


@pytest.mark.parametrize(
    "codec,expected_ext",
    [
        (CodecEnum.MHM1, ".mp4"),   # Sony 360RA
        (CodecEnum.MHA1, ".m4a"),   # Sony 360RA
        (CodecEnum.EAC3, ".m4a"),   # Dolby Atmos
        (CodecEnum.AC4, ".m4a"),    # Dolby Atmos
        (CodecEnum.AC3, ".ac3"),    # Dolby Digital
        (CodecEnum.FLAC, ".flac"),  # non-spatial control
    ],
)
def test_spatial_codecs_produce_expected_filename_extension(codec, expected_ext):
    """override_codec drives the extension when the module remuxes the container."""
    downloader = _minimal_downloader()
    track = _make_track(codec)
    filename = _track_filename(downloader, track, override_codec=codec)
    assert filename.endswith(expected_ext), f"{codec.name} -> {filename}"


@pytest.mark.parametrize(
    "codec,expected_ext",
    [
        (CodecEnum.MHM1, ".mp4"),
        (CodecEnum.MHA1, ".m4a"),
    ],
)
def test_spatial_codec_from_track_info_without_override(codec, expected_ext):
    """Without an override_codec, track_info.codec still yields the right extension."""
    downloader = _minimal_downloader()
    filename = _track_filename(downloader, _make_track(codec))
    assert filename.endswith(expected_ext), f"{codec.name} -> {filename}"


def test_override_codec_takes_precedence_over_track_info():
    """When a module remuxes to a different container, the override wins."""
    downloader = _minimal_downloader()
    track = _make_track(CodecEnum.FLAC)  # track says FLAC...
    filename = _track_filename(downloader, track, override_codec=CodecEnum.MHM1)
    assert filename.endswith(".mp4")  # ...but the module remuxed to MP4


def test_filename_template_still_applies():
    """The extension is appended to the rendered template, not replacing it."""
    downloader = _minimal_downloader()
    filename = _track_filename(downloader, _make_track(CodecEnum.MHM1))
    assert filename.startswith("Test Artist - Test Track")


# ---------------------------------------------------------------------------
# 3. Quality labels (folder names) stay consistent with the new extensions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "codec,expected_label",
    [
        (CodecEnum.MHA1, "360RA"),   # Sony 360RA -> 360 Reality Audio
        (CodecEnum.MHM1, "360RA"),   # Sony 360RA -> 360 Reality Audio
        (CodecEnum.EAC3, "ATMOS"),   # Dolby Atmos
        (CodecEnum.AC4, "ATMOS"),    # Dolby Atmos
        (CodecEnum.FLAC, "FLAC"),    # non-spatial control
    ],
)
def test_quality_label_distinguishes_360ra_from_atmos(codec, expected_label):
    """Folder quality labels must not collapse 360RA into ATMOS."""
    downloader = _minimal_downloader()
    track = _make_track(codec)
    assert Downloader._derive_track_quality_label(track) == expected_label


def test_quality_path_label_for_360ra_is_short_label():
    """The folder label for 360RA is the short '360RA', not the long form."""
    label = Downloader._quality_path_label("360RA")
    assert label == "360RA"


def test_quality_path_label_for_atmos_keeps_atmos():
    """Atmos folder labels stay ATMOS (not 360RA)."""
    label = Downloader._quality_path_label("ATMOS")
    assert "ATMOS" in label
    assert "360" not in label


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
