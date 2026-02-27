"""FunASR protocol adapter unit tests."""

import json

import pytest

from app.adapters.base import MessageProfile, RecognitionMode, ServerType
from app.adapters.funasr_ws import FunASRWebSocketAdapter


@pytest.fixture
def adapter():
    return FunASRWebSocketAdapter(server_type=ServerType.FUNASR_MAIN)


@pytest.fixture
def legacy_adapter():
    return FunASRWebSocketAdapter(server_type=ServerType.LEGACY)


@pytest.mark.unit
class TestBuildStartMessage:
    def test_offline_new_server(self, adapter):
        profile = MessageProfile(mode=RecognitionMode.OFFLINE, wav_name="test.wav")
        msg = json.loads(adapter.build_start_message(profile))
        assert msg["mode"] == "offline"
        assert msg["is_speaking"] is True
        assert msg["wav_name"] == "test.wav"

    def test_new_server_includes_svs_params(self, adapter):
        profile = MessageProfile(mode=RecognitionMode.OFFLINE, wav_name="test.wav")
        msg = json.loads(adapter.build_start_message(profile))
        assert "svs_lang" in msg
        assert "svs_itn" in msg

    def test_legacy_server_no_svs_params(self, legacy_adapter):
        profile = MessageProfile(mode=RecognitionMode.OFFLINE, wav_name="test.wav")
        msg = json.loads(legacy_adapter.build_start_message(profile))
        assert "svs_lang" not in msg

    def test_hotwords_included(self, adapter):
        profile = MessageProfile(wav_name="x.wav", hotwords="会议,决议")
        msg = json.loads(adapter.build_start_message(profile))
        assert msg["hotwords"] == "会议,决议"

    def test_online_mode_chunk_params(self, adapter):
        profile = MessageProfile(mode=RecognitionMode.ONLINE, wav_name="stream.wav")
        msg = json.loads(adapter.build_start_message(profile))
        assert "chunk_size" in msg
        assert "chunk_interval" in msg


@pytest.mark.unit
class TestParseResult:
    def test_parse_normal_response(self, adapter):
        raw = json.dumps({"text": "你好世界", "mode": "offline", "is_final": False})
        result = adapter.parse_result(raw)
        assert result.text == "你好世界"
        assert result.is_complete is True

    def test_parse_stamp_sents(self, adapter):
        raw = json.dumps({"text": "", "stamp_sents": [{"text_seg": "你好", "ts": [0, 500]}, {"text_seg": "世界", "ts": [500, 1000]}], "mode": "offline"})
        result = adapter.parse_result(raw)
        assert result.text == "你好世界"

    def test_parse_2pass_offline(self, adapter):
        raw = json.dumps({"text_2pass_offline": "2pass结果", "mode": "2pass-offline"})
        result = adapter.parse_result(raw)
        assert result.text == "2pass结果"
        assert result.is_complete is True

    def test_parse_invalid_json(self, adapter):
        result = adapter.parse_result("not json")
        assert result.error is not None


@pytest.mark.unit
class TestShouldComplete:
    def test_new_server_offline_is_final_false_still_complete(self, adapter):
        raw = json.dumps({"text": "结果", "mode": "offline", "is_final": False})
        result = adapter.parse_result(raw)
        assert result.is_final is False
        assert result.is_complete is True

    def test_legacy_server_is_final_true(self, legacy_adapter):
        raw = json.dumps({"text": "结果", "mode": "offline", "is_final": True})
        result = legacy_adapter.parse_result(raw)
        assert result.is_final is True
        assert result.is_complete is True

    def test_online_not_complete_without_is_final(self, adapter):
        raw = json.dumps({"text": "部分", "mode": "online", "is_final": False})
        result = adapter.parse_result(raw)
        assert result.is_complete is False


@pytest.mark.unit
class TestCoerceBool:
    @pytest.mark.parametrize("value,expected", [(True, True), (False, False), (1, True), (0, False), ("true", True), ("True", True), ("false", False), ("1", True), ("0", False), ("yes", True), (None, False)])
    def test_coerce_bool_variants(self, value, expected):
        assert FunASRWebSocketAdapter._coerce_bool(value) == expected


@pytest.mark.unit
class TestBuildEndMessage:
    def test_end_message(self, adapter):
        msg = json.loads(adapter.build_end_message())
        assert msg["is_speaking"] is False
