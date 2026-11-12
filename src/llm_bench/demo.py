"""A synthetic first run using the real parsing, metrics and report pipeline.

No provider files, environment credentials or network transports are used.
The timings below are scripted examples, never performance measurements.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from llm_bench.models import CacheProfile, ProviderConfig, ResolvedConfig, TestProfile
from llm_bench.runner import Runner
from llm_bench.transport.http import FakeResponse, FakeTransport


def _response(ttft: float | None, cache_read: int = 0) -> FakeResponse:
    events = [(10.0, 'data: {"choices":[{"delta":{"role":"assistant"}}]}')]
    if ttft is not None:
        events.append((ttft, 'data: {"choices":[{"delta":{"content":"Demo"}}]}'))
    events += [
        (500.0, "data: " + json.dumps({
            "choices": [],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 4,
                      "prompt_tokens_details": {"cached_tokens": cache_read}},
        })),
        (510.0, "data: [DONE]"),
    ]
    return FakeResponse(sse_events=events, total_ms=520.0)


def _transport() -> FakeTransport:
    # Concurrency is one in this demonstration: response order is stable.
    latency = iter([
        _response(120.0), _response(240.0),
        FakeResponse(status=429, error="Synthetic rate limit", total_ms=50.0,
                     raw_error_text='{"error":"synthetic rate limit"}'),
        _response(None),  # HTTP 200 without a semantic delta: TTFT is missing.
    ])
    cache = iter([_response(300.0, 100), _response(80.0, 800),
                  _response(90.0, 800), _response(100.0, 100)])

    def select(test_name: str, _method: str, _url: str) -> FakeResponse:
        if test_name.startswith("stream_preflight_"):
            return _response(20.0)
        if test_name.startswith("latency_"):
            return next(latency)
        if test_name.startswith("cache_"):
            return next(cache)
        raise ValueError(f"Unexpected demo request: {test_name}")

    return FakeTransport(selector=select)


def run_demo(output_root: Path) -> dict[str, Any]:
    """Create two independent runs; existing files are never overwritten."""
    output_root = output_root.expanduser().resolve()
    result: dict[str, Any] = {
        "command": "demo",
        "data_kind": "synthetic",
        "network_requests_executed": False,
        "note": "Scripted responses and timings. One simulated HTTP 429 per run.",
        "runs": {},
    }
    for semantics in ("includes_cache", "unknown"):
        provider = ProviderConfig(
            id=f"demo-{semantics}", display_name="Synthetic offline demo",
            base_url="https://demo.invalid", default_model="demo-model",
            protocols=["openai"], api_key_env="DEMO_NOT_USED",
            input_token_semantics={"openai": semantics},
        )
        profile = TestProfile(
            suites=["latency", "cache"], timeout_seconds=1,
            input_sizes=[256], concurrency_levels=[1], samples_per_group=4,
            reasoning_samples=1, max_output_tokens=8,
            cache=CacheProfile(mode="openai_observation", cold_start_requests=1,
                               warm_requests=3),
        )
        runner = Runner(ResolvedConfig(provider, "demo-model", profile), _transport())
        # Keep the first-run output concise. All evidence remains in the run.
        with redirect_stdout(io.StringIO()):
            _, paths = runner.run(output_root, api_key="", synthetic=True)
        result["runs"][semantics] = {
            "run_dir": paths.root.as_posix(),
            "report": (paths.report / "benchmark_report.md").as_posix(),
            "summary": (paths.normalized / "summary.json").as_posix(),
            "requests_csv": (paths.normalized / "requests.csv").as_posix(),
            "raw": paths.raw_requests.as_posix(),
        }
    return result
