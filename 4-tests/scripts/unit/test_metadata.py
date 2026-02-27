"""Metadata extraction unit tests."""

import pytest

from app.services.metadata import _parse_ffprobe_output


@pytest.mark.unit
class TestParseFFprobeOutput:
    def test_parse_wav_output(self):
        data = {"format": {"duration": "5.0", "format_name": "wav"}, "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le", "sample_rate": "16000", "channels": 1}]}
        meta = _parse_ffprobe_output(data)
        assert meta.duration_sec == pytest.approx(5.0)
        assert meta.codec == "pcm_s16le"
        assert meta.sample_rate == 16000
        assert meta.channels == 1
        assert meta.media_type == "audio"
        assert meta.mime == "audio/wav"

    def test_parse_mp3_output(self):
        data = {"format": {"duration": "60.0", "format_name": "mp3"}, "streams": [{"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100", "channels": 2}]}
        meta = _parse_ffprobe_output(data)
        assert meta.duration_sec == pytest.approx(60.0)
        assert meta.sample_rate == 44100
        assert meta.mime == "audio/mpeg"

    def test_parse_mp4_output(self):
        data = {"format": {"duration": "30.0", "format_name": "mov,mp4,m4a,dash,gp3"}, "streams": [{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}]}
        meta = _parse_ffprobe_output(data)
        assert meta.media_type == "video"
        assert meta.codec == "h264"
        assert meta.sample_rate == 48000
        assert meta.mime == "video/mp4"

    def test_parse_flac_output(self):
        data = {"format": {"duration": "120.0", "format_name": "flac"}, "streams": [{"codec_type": "audio", "codec_name": "flac", "sample_rate": "96000", "channels": 2}]}
        meta = _parse_ffprobe_output(data)
        assert meta.codec == "flac"
        assert meta.mime == "audio/flac"

    def test_parse_empty_streams(self):
        data = {"format": {"duration": "0"}, "streams": []}
        meta = _parse_ffprobe_output(data)
        assert meta.media_type == "unknown"

    def test_album_art_not_treated_as_video(self):
        data = {"format": {"duration": "180.0", "format_name": "mp3"}, "streams": [{"codec_type": "video", "codec_name": "mjpeg"}, {"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100", "channels": 2}]}
        meta = _parse_ffprobe_output(data)
        assert meta.media_type == "audio"
