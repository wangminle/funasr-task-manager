"""Pydantic schemas for ASR server management."""

from datetime import datetime

from pydantic import BaseModel


class ServerRegisterRequest(BaseModel):
    server_id: str
    name: str | None = None
    host: str
    port: int
    protocol_version: str
    max_concurrency: int = 4
    labels: dict | None = None


class ServerUpdateRequest(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    protocol_version: str | None = None
    max_concurrency: int | None = None
    labels: dict | None = None


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


class ServerBenchmarkItem(BaseModel):
    server_id: str
    reachable: bool = False
    responsive: bool = False
    error: str | None = None
    single_rtf: float | None = None
    throughput_rtf: float | None = None
    benchmark_concurrency: int | None = None
    benchmark_audio_sec: float | None = None
    benchmark_elapsed_sec: float | None = None
    benchmark_samples: list[str] = []
    benchmark_notes: list[str] = []
    concurrency_gradient: list[ConcurrencyGradientItem] = []


class ServerCapacityItem(BaseModel):
    server_id: str
    rtf: float
    relative_speed: float
    acceleration_ratio: float


class ServerBenchmarkResponse(BaseModel):
    results: list[ServerBenchmarkItem]
    capacity_comparison: list[ServerCapacityItem]
