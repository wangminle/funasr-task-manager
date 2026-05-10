"""Pydantic schemas for ASR server management."""

from datetime import datetime

from pydantic import BaseModel, field_validator

_PROTOCOL_ALIASES = {
    "funasr-main": "v2_new",
    "funasr-legacy": "v1_old",
}


def _normalize_protocol(v: str) -> str:
    return _PROTOCOL_ALIASES.get(v, v)


class ServerRegisterRequest(BaseModel):
    server_id: str
    name: str | None = None
    host: str
    port: int
    protocol_version: str
    max_concurrency: int = 4
    labels: dict | None = None
    run_benchmark: bool = False

    @field_validator("protocol_version")
    @classmethod
    def _normalize_pv(cls, v: str) -> str:
        return _normalize_protocol(v)


class ServerUpdateRequest(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    protocol_version: str | None = None
    max_concurrency: int | None = None
    enabled: bool | None = None
    labels: dict | None = None

    @field_validator("protocol_version")
    @classmethod
    def _normalize_pv(cls, v: str | None) -> str | None:
        return _normalize_protocol(v) if v else v

    @field_validator("enabled", mode="before")
    @classmethod
    def _reject_null_enabled(cls, v: bool | None) -> bool:
        if v is None:
            raise ValueError("enabled cannot be null; omit the field or use true/false")
        return v


class ServerResponse(BaseModel):
    server_id: str
    name: str | None = None
    host: str
    port: int
    protocol_version: str
    server_type: str | None = None
    supported_modes: str | None = None
    max_concurrency: int
    rtf_baseline: float | None = None
    throughput_rtf: float | None = None
    benchmark_concurrency: int | None = None
    penalty_factor: float = 0.1
    status: str
    enabled: bool = True
    last_heartbeat: datetime | None = None

    model_config = {"from_attributes": True}


class ServerProbeResponse(BaseModel):
    server_id: str
    reachable: bool = False
    responsive: bool = False
    error: str | None = None
    supports_offline: bool | None = None
    supports_online: bool | None = None
    supports_2pass: bool | None = None
    has_timestamp: bool = False
    has_stamp_sents: bool = False
    is_final_semantics: str = "unknown"
    inferred_server_type: str = "unknown"
    probe_level: str = "CONNECT_ONLY"
    probe_notes: list[str] = []
    probe_duration_ms: float = 0.0


class ConcurrencyGradientItem(BaseModel):
    concurrency: int
    per_file_rtf: float
    throughput_rtf: float
    wall_clock_sec: float
    total_audio_sec: float
    # V2 timing breakdown
    avg_connect_ms: float = 0.0
    avg_upload_ms: float = 0.0
    upload_spread_ms: float = 0.0
    avg_post_upload_wait_ms: float = 0.0
    max_post_upload_wait_ms: float = 0.0
    concurrent_post_upload_ms: float = 0.0
    avg_first_response_ms: float = 0.0
    server_per_file_rtf: float = 0.0
    server_throughput_rtf: float = 0.0
    ping_rtt_ms: float | None = None


class ServerBenchmarkItem(BaseModel):
    server_id: str
    reachable: bool = False
    responsive: bool = False
    error: str | None = None
    single_rtf: float | None = None
    throughput_rtf: float | None = None
    benchmark_concurrency: int | None = None
    recommended_concurrency: int | None = None
    benchmark_audio_sec: float | None = None
    benchmark_elapsed_sec: float | None = None
    benchmark_samples: list[str] = []
    benchmark_notes: list[str] = []
    gradient_complete: bool = True
    concurrency_gradient: list[ConcurrencyGradientItem] = []


class ServerCapacityItem(BaseModel):
    server_id: str
    rtf: float
    relative_speed: float
    acceleration_ratio: float


class ServerBenchmarkResponse(BaseModel):
    results: list[ServerBenchmarkItem]
    capacity_comparison: list[ServerCapacityItem]
