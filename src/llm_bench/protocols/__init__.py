"""Protocol adapters turn provider-neutral requests into wire payloads and
wire responses (SSE/usage) into neutral metrics."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from llm_bench.models import BenchError
from llm_bench.transport.sse import SseStream


@dataclass
class StreamMetrics:
    """Neutral streaming outcome extracted from an SSE stream."""

    first_event_ms: float | None = None
    first_reasoning_ms: float | None = None
    first_content_ms: float | None = None
    ttft_ms: float | None = None
    ttft_source: str | None = None          # reasoning | content
    missing_reason: str | None = None       # e.g. no_semantic_delta
    reasoning_chars: int = 0
    content_chars: int = 0
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def ttft_valid(self) -> bool:
        return self.ttft_ms is not None


class Protocol:
    name: str = ""
    sse_label: str = ""

    def headers(
        self, api_key: str, request_id: str, stream: bool, options: dict[str, Any]
    ) -> dict[str, str]:
        raise NotImplementedError

    def usage_fields(self, usage: dict[str, Any]) -> dict[str, Any]:
        """Map a provider usage object into neutral token fields."""
        raise NotImplementedError

    @property
    def input_token_semantics(self) -> str:
        raise NotImplementedError

    def parse_stream(self, stream: SseStream) -> StreamMetrics:
        raise NotImplementedError


_REGISTRY: dict[str, Protocol] = {}


def register(protocol: Protocol) -> Protocol:
    _REGISTRY[protocol.name] = protocol
    return protocol


def get_protocol(name: str) -> Protocol:
    if name not in _REGISTRY:
        raise BenchError(f"未知协议 `{name}`，可用：{sorted(_REGISTRY)}")
    return _REGISTRY[name]


def available_protocols() -> list[str]:
    return sorted(_REGISTRY)


# Importing the modules triggers registration.
from llm_bench.protocols import anthropic as _anthropic  # noqa: E402,F401
from llm_bench.protocols import openai as _openai  # noqa: E402,F401
