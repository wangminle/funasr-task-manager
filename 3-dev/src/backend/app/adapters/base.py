"""Abstract base class for ASR protocol adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ServerType(StrEnum):
    AUTO = "auto"
    LEGACY = "legacy"
    FUNASR_MAIN = "funasr_main"


class RecognitionMode(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"
    TWOPASS = "2pass"


@dataclass
class MessageProfile:
    server_type: ServerType = ServerType.AUTO
    mode: RecognitionMode = RecognitionMode.OFFLINE
    wav_name: str = "audio"
    wav_format: str = "pcm"
    audio_fs: int = 16000
    use_itn: bool = True
    hotwords: str = ""
    enable_svs_params: bool = False
    svs_lang: str = "auto"
    svs_itn: bool = True
    chunk_size: list[int] = field(default_factory=lambda: [5, 10, 5])
    chunk_interval: int = 10


@dataclass
class ParsedResult:
    text: str = ""
    mode: str = ""
    wav_name: str = ""
    is_final: bool = False
    is_complete: bool = False
    timestamp: Any = None
    stamp_sents: Any = None
    raw: dict = field(default_factory=dict)
    raw_string: str = ""
    error: str | None = None


class BaseAdapter(ABC):
    @abstractmethod
    def build_start_message(self, profile: MessageProfile) -> str: ...
    @abstractmethod
    def build_end_message(self) -> str: ...
    @abstractmethod
    def parse_result(self, raw_msg: str) -> ParsedResult: ...
    @abstractmethod
    async def transcribe(self, host: str, port: int, audio_path: str, profile: MessageProfile, *, use_ssl: bool = True, timeout: float = 300.0) -> ParsedResult: ...
