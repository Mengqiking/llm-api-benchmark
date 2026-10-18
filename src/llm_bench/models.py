"""Core data models: provider config, test profile, normalized request rows."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SUPPORTED_PROTOCOLS = ("openai", "anthropic")
SUPPORTED_SUITES = ("smoke", "reasoning", "latency", "cache", "video")
INPUT_TOKEN_SEMANTICS = ("excludes_cache", "includes_cache", "unknown")
CACHE_MODES = ("auto", "anthropic_explicit", "openai_observation")
CACHE_HIT_SCOPES = ("none", "any", "target_prefix", "unknown")

TTFT_MISSING_REASONS = (
    "no_semantic_delta",       # HTTP 2xx but no reasoning/content delta arrived
    "http_error",
    "transport_error",
    "non_streaming_request",
    "not_recorded",            # legacy data without TTFT measurement
)


class BenchError(Exception):
    """Base error for user-facing toolkit failures (config, data, IO)."""


@dataclass
class ProviderConfig:
    id: str
    display_name: str
    base_url: str
    default_model: str
    protocols: list[str]
    api_key_env: str
    extra_headers: dict[str, str] = field(default_factory=dict)
    protocol_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    # protocol -> excludes_cache | includes_cache | unknown
    input_token_semantics: dict[str, str] = field(default_factory=dict)
    cache_metric_source: str = "auto"
    gateway_cache_fields: list[str] = field(default_factory=list)

    def semantics_for(self, protocol: str) -> str:
        return self.input_token_semantics.get(protocol, "unknown")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CacheProfile:
    mode: str = "auto"
    # Streaming cache probes carry per-request TTFT; non-streaming probes have
    # no TTFT and the report must say so instead of leaving it ambiguous.
    stream: bool = True
    cold_start_requests: int = 1
    warm_requests: int = 4
    unique_prefix_per_run: bool = True
    varying_suffix: bool = True

    @property
    def total_requests(self) -> int:
        return self.cold_start_requests + self.warm_requests


@dataclass
class TestProfile:
    suites: list[str]
    timeout_seconds: int
    input_sizes: list[int]
    concurrency_levels: list[int]
    samples_per_group: int
    reasoning_samples: int
    max_output_tokens: int
    cache: CacheProfile = field(default_factory=CacheProfile)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


@dataclass
class ResolvedConfig:
    provider: ProviderConfig
    model: str
    profile: TestProfile

    @property
    def protocols(self) -> list[str]:
        return list(self.provider.protocols)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.to_dict(),
            "model": self.model,
            "profile": self.profile.to_dict(),
        }


@dataclass
class NormalizedRequest:
    """One request in the canonical normalized form shared by runs and reports."""

    schema_version: int
    request_id: str
    test_name: str
    suite: str
    protocol: str
    provider: str
    model: str
    started_at: str | None = None
    completed_at: str | None = None
    success: bool = False
    http_status: int | None = None
    error: str | None = None
    input_size_chars: int | None = None
    concurrency: int | None = None
    reasoning_effort: str | None = None
    sequence: int | None = None
    phase: str | None = None  # cache probes: cold_start | warm
    stream: bool = False
    # TTFT fields
    request_started_at: str | None = None
    first_byte_ms: float | None = None
    first_event_ms: float | None = None
    first_reasoning_ms: float | None = None
    first_content_ms: float | None = None
    ttft_ms: float | None = None
    total_ms: float | None = None
    ttft_source: str | None = None  # reasoning | content | unknown | None
    ttft_valid: bool = False
    ttft_missing_reason: str | None = None
    # cache fields
    input_tokens_raw: int | None = None
    uncached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    total_logical_input_tokens: int | None = None
    input_token_semantics: str = "unknown"
    cache_metric_source: str | None = None
    cache_hit: bool = False
    cache_hit_scope: str = "none"
    cache_metrics_valid: bool = False
    cache_metrics_warning: str | None = None
    # target-prefix evidence: warm requests must exceed the cold-start baseline
    cold_baseline_cache_read_tokens: int | None = None
    target_prefix_cache_read_tokens: int | None = None
    target_prefix_evidence: bool | None = None
    # token counters
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    reasoning_chars: int = 0
    content_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NormalizedRequest":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})
