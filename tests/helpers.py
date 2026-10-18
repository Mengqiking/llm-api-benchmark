"""Shared helpers for building synthetic normalized rows in tests."""
from __future__ import annotations

from typing import Any


def make_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "request_id": "req_1",
        "test_name": "cache",
        "suite": "cache",
        "protocol": "anthropic",
        "provider": "tostco",
        "model": "kimi-k3",
        "started_at": "2026-10-15T10:00:00.000+08:00",
        "completed_at": "2026-10-15T10:00:01.000+08:00",
        "success": True,
        "http_status": 200,
        "error": None,
        "input_size_chars": None,
        "concurrency": None,
        "reasoning_effort": None,
        "sequence": 1,
        "phase": None,
        "stream": False,
        "request_started_at": "2026-10-15T10:00:00.000+08:00",
        "first_byte_ms": 50.0,
        "first_event_ms": None,
        "first_reasoning_ms": None,
        "first_content_ms": None,
        "ttft_ms": 100.0,
        "total_ms": 500.0,
        "ttft_source": None,
        "ttft_valid": False,
        "ttft_missing_reason": "non_streaming_request",
        "input_tokens_raw": 1705,
        "uncached_input_tokens": 1705,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 64,
        "total_logical_input_tokens": 1769,
        "input_token_semantics": "excludes_cache",
        "cache_metric_source": "usage.cache_read_input_tokens",
        "cache_hit": True,
        "cache_hit_scope": "any",
        "cache_metrics_valid": True,
        "cache_metrics_warning": None,
        "cold_baseline_cache_read_tokens": None,
        "target_prefix_cache_read_tokens": None,
        "target_prefix_evidence": None,
        "output_tokens": 8,
        "thinking_tokens": 0,
        "reasoning_chars": 0,
        "content_chars": 0,
    }
    base.update(overrides)
    return base


def sse_pairs_from_fixture(path: Path) -> list[tuple[float, str]]:
    """Read a text fixture of SSE lines and attach synthetic timings:
    the n-th line arrives at n*100 ms."""
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [((index + 1) * 100.0, line) for index, line in enumerate(lines)]
