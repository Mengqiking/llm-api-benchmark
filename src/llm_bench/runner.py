"""Benchmark orchestration: executes suites through an injectable transport.

`UrllibTransport` performs real network calls (command `run`); tests and
offline paths inject `FakeTransport`, so the same code path is exercised
without any network access.
"""
from __future__ import annotations

import json
import platform
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm_bench import SCHEMA_VERSION, TOOL_VERSION
from llm_bench.metrics.cache import normalize_cache_fields
from llm_bench.models import NormalizedRequest, ResolvedConfig
from llm_bench.protocols import get_protocol
from llm_bench.store import RunPaths, RunStore, make_run_paths, now_iso
from llm_bench.transport.http import UrllibTransport
from llm_bench.transport.sse import parse_sse_lines

TEST_SUITE = {
    "smoke_models": "smoke",
    "smoke_chat": "smoke",
    "smoke_anthropic": "smoke",
    "thinking_disable": "smoke",
    "stream_preflight": "preflight",
    "reasoning_effort": "reasoning",
    "latency": "latency",
    "cache": "cache",
    "video_probe": "video",
    "video_probe_retry": "video",
}

VIDEO_SAMPLE_URL = "https://www.w3schools.com/html/mov_bbb.mp4"


def new_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"


def is_success(record: dict[str, Any]) -> bool:
    status = record.get("http_status")
    return record.get("error") is None and isinstance(status, int) and 200 <= status < 300


class Runner:
    def __init__(self, resolved: ResolvedConfig, transport: Any, run_id: str | None = None) -> None:
        self.config = resolved
        self.provider = resolved.provider
        self.model = resolved.model
        self.profile = resolved.profile
        self.transport = transport
        self.run_id = run_id or new_run_id()

    # ------------------------------------------------------------------ utils
    def _options(self, protocol_name: str) -> dict[str, Any]:
        return self.provider.protocol_options.get(protocol_name, {})

    def _latency_prompt(self, size: int) -> str:
        fixed = ("alpha beta gamma delta " * (size // 23 + 1))[:size]
        return f"以下内容仅用于性能测试：\n{fixed}\n只回复OK。"

    def _cache_prefix(self) -> str:
        if self.profile.cache.unique_prefix_per_run:
            return f"llmbench-cache-{self.provider.id}-{self.run_id}-" + "固定资料段落。" * 400
        return "llmbench-cache-shared-prefix-" + "固定资料段落。" * 400

    @staticmethod
    def _cache_suffix(sequence: int, varying: bool = True) -> str:
        return f"请求编号{sequence}，只回复OK" if varying else "只回复OK"

    # ------------------------------------------------------------- execution
    def _request(
        self,
        store: RunStore,
        api_key: str,
        *,
        name: str,
        suite: str,
        protocol_name: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        stream: bool,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        protocol = get_protocol(protocol_name)
        options = self._options(protocol_name)
        request_id = f"{name}_{uuid.uuid4().hex}"
        url = f"{self.provider.base_url}{path}"
        headers = protocol.headers(api_key, request_id, stream, options)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "test_name": name,
            "suite": suite,
            "protocol": protocol_name,
            "provider": self.provider.id,
            "model": self.model,
            "started_at": now_iso(),
            "method": method,
            "url": url,
            "request_headers": headers,
            "request_body": payload,
            "metadata": metadata or {},
            "stream": stream,
        }
        if stream:
            outcome = self.transport.stream(method, url, headers, body)
            record.update(
                http_status=outcome.status,
                response_headers=outcome.headers,
                headers_ms=outcome.headers_ms,
                total_ms=outcome.total_ms,
                completed_at=now_iso(),
                error=outcome.error,
                raw_sse_events=[{"elapsed_ms": ms, "line": line} for ms, line in outcome.events],
            )
            if outcome.error:
                record["raw_response"] = outcome.raw_error_text
                record["ttft_missing_reason"] = (
                    "http_error" if outcome.status is not None else "transport_error"
                )
                sse = parse_sse_lines([])
            else:
                record["raw_response"] = ""
                sse = parse_sse_lines(outcome.events)
            metrics = protocol.parse_stream(sse)
            record.update(
                first_event_ms=metrics.first_event_ms,
                first_reasoning_ms=metrics.first_reasoning_ms,
                first_content_ms=metrics.first_content_ms,
                ttft_ms=metrics.ttft_ms,
                ttft_source=metrics.ttft_source,
                reasoning_chars=metrics.reasoning_chars,
                content_chars=metrics.content_chars,
                usage=metrics.usage,
                ttft_valid=metrics.ttft_valid,
            )
            if not metrics.ttft_valid and "ttft_missing_reason" not in record:
                record["ttft_missing_reason"] = metrics.missing_reason
        else:
            outcome = self.transport.json_request(method, url, headers, body)
            record.update(
                http_status=outcome.status,
                response_headers=outcome.headers,
                headers_ms=outcome.headers_ms,
                total_ms=outcome.total_ms,
                raw_response=outcome.raw_text,
                parsed_response=outcome.parsed,
                completed_at=now_iso(),
                error=outcome.error,
                ttft_valid=False,
                ttft_missing_reason="non_streaming_request",
            )
            if outcome.error is None and isinstance(outcome.parsed, dict):
                record["usage"] = outcome.parsed.get("usage") or {}
        record["request_started_at"] = record["started_at"]
        store.save(record)
        status = record.get("http_status")
        ok = is_success(record)
        print(
            f"[{name}] HTTP {status if status is not None else 'N/A'} "
            f"{'OK' if ok else str(record.get('error') or 'error')[:120]}",
            flush=True,
        )
        return record

    # ---------------------------------------------------------------- suites
    def run_smoke(self, store: RunStore, api_key: str) -> None:
        if "openai" in self.config.protocols:
            proto = get_protocol("openai")
            opts = self._options("openai")
            self._request(
                store, api_key, name="smoke_models", suite="smoke", protocol_name="openai",
                method="GET", path=proto.models_path(opts), payload=None, stream=False,
            )
            self._request(
                store, api_key, name="smoke_chat", suite="smoke", protocol_name="openai",
                method="POST", path=proto.chat_path(opts),
                payload=proto.chat_payload(self.model, "只回复OK", max_tokens=64, stream=False, options=opts),
                stream=False,
            )
        if "anthropic" in self.config.protocols:
            proto = get_protocol("anthropic")
            opts = self._options("anthropic")
            self._request(
                store, api_key, name="smoke_anthropic", suite="smoke", protocol_name="anthropic",
                method="POST", path=proto.messages_path(opts),
                payload=proto.messages_payload(self.model, "只回复OK", max_tokens=64),
                stream=False,
            )
            self._request(
                store, api_key, name="thinking_disable", suite="smoke", protocol_name="anthropic",
                method="POST", path=proto.messages_path(opts),
                payload=proto.messages_payload(self.model, "只回复OK", max_tokens=64, thinking_disabled=True),
                stream=False,
            )

    def _stream_preflight_ok(self, store: RunStore, api_key: str) -> bool:
        proto = get_protocol("openai")
        opts = self._options("openai")
        record = self._request(
            store, api_key, name="stream_preflight", suite="preflight", protocol_name="openai",
            method="POST", path=proto.chat_path(opts),
            payload=proto.chat_payload(self.model, "只回复OK", max_tokens=32, effort="low", stream=True, options=opts),
            stream=True,
            metadata={"purpose": "preflight"},
        )
        return is_success(record)

    def run_reasoning(self, store: RunStore, api_key: str) -> None:
        proto = get_protocol("openai")
        opts = self._options("openai")
        # One explicit off probe turns the reasoning suite into a capability
        # test: can this gateway accept an independent-thinking off switch?
        self._request(
            store, api_key, name="reasoning_switch_off", suite="reasoning", protocol_name="openai",
            method="POST", path=proto.chat_path(opts),
            payload=proto.chat_payload(
                self.model, "计算 37×41，只给出最终整数。", max_tokens=96, effort="none",
                stream=True, options=opts,
            ),
            stream=True,
            metadata={"reasoning_effort": "none", "sequence": 1, "purpose": "reasoning_switch_off"},
        )
        for effort in ("low", "high", "max"):
            for sequence in range(1, self.profile.reasoning_samples + 1):
                self._request(
                    store, api_key, name="reasoning_effort", suite="reasoning", protocol_name="openai",
                    method="POST", path=proto.chat_path(opts),
                    payload=proto.chat_payload(
                        self.model, "计算 37×41，只给出最终整数。", max_tokens=96, effort=effort,
                        stream=True, options=opts,
                    ),
                    stream=True,
                    metadata={"reasoning_effort": effort, "sequence": sequence},
                )

    def run_latency_group(
        self, store: RunStore, api_key: str, prompt: str, size: int, concurrency: int, count: int
    ) -> None:
        proto = get_protocol("openai")
        opts = self._options("openai")
        payload = proto.chat_payload(
            self.model, prompt, max_tokens=self.profile.max_output_tokens, effort="low",
            stream=True, options=opts,
        )

        def one(sequence: int) -> None:
            self._request(
                store, api_key, name="latency", suite="latency", protocol_name="openai",
                method="POST", path=proto.chat_path(opts), payload=payload, stream=True,
                metadata={"input_size_chars": size, "concurrency": concurrency, "sequence": sequence},
            )

        print(f"[latency] input={size} concurrency={concurrency} requests={count}", flush=True)
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(one, i) for i in range(1, count + 1)]
            for future in as_completed(futures):
                future.result()

    def _cache_protocol(self) -> str:
        if self.profile.cache.mode == "openai_observation":
            return "openai"
        if self.profile.cache.mode == "anthropic_explicit":
            return "anthropic"
        return "anthropic" if "anthropic" in self.config.protocols else "openai"

    def run_cache(self, store: RunStore, api_key: str) -> None:
        cache = self.profile.cache
        protocol_name = self._cache_protocol()
        proto = get_protocol(protocol_name)
        options = self._options(protocol_name)
        prefix = self._cache_prefix()
        # Streaming probes carry both TTFT and (via the usage tail event) the
        # cache token counters; non-streaming probes give usage but no TTFT.
        stream = bool(cache.stream)
        for sequence in range(1, cache.total_requests + 1):
            if protocol_name == "anthropic":
                path = proto.messages_path(options)
                payload = proto.cache_payload(
                    self.model, prefix, self._cache_suffix(sequence, cache.varying_suffix),
                    mode=cache.mode, max_tokens=8, stream=stream,
                )
            else:
                path = proto.chat_path(options)
                payload = proto.chat_payload(
                    self.model, f"{prefix}\n{self._cache_suffix(sequence, cache.varying_suffix)}",
                    max_tokens=8, effort="low", stream=stream, options=options,
                )
            phase = "cold_start" if sequence <= cache.cold_start_requests else "warm"
            record = self._request(
                store, api_key, name="cache", suite="cache", protocol_name=protocol_name,
                method="POST", path=path, payload=payload, stream=stream,
                metadata={
                    "sequence": sequence,
                    "cache_mode": cache.mode,
                    "stream": stream,
                    "phase": phase,
                },
            )
            if sequence == 1 and not is_success(record):
                print("[suite] 首个缓存请求失败，跳过剩余缓存请求", flush=True)
                break

    def run_video(self, store: RunStore, api_key: str) -> None:
        proto = get_protocol("openai")
        opts = self._options("openai")
        self._request(
            store, api_key, name="video_probe", suite="video", protocol_name="openai",
            method="POST", path=proto.chat_path(opts),
            payload=proto.video_payload(self.model, VIDEO_SAMPLE_URL, max_tokens=128),
            stream=False,
            metadata={"sample": "W3Schools mov_bbb.mp4"},
        )

    # ------------------------------------------------------------- normalize
    @staticmethod
    def _cold_baseline_cache_read(records: list[dict[str, Any]], first_success_sequence: int | None) -> int | None:
        """Highest cache_read on successful cold-phase cache records; warm
        probes must exceed it to count as target-prefix evidence."""
        if first_success_sequence is None:
            return None
        reads: list[int] = []
        for record in records:
            if record.get("test_name") != "cache" or not is_success(record):
                continue
            metadata = record.get("metadata") or {}
            seq = metadata.get("sequence")
            if seq is None:
                continue
            phase = metadata.get("phase")
            in_cold = phase == "cold_start" or (
                phase is None and int(seq) <= first_success_sequence
            )
            if not in_cold:
                continue
            usage = record.get("usage") or {}
            raw = (
                get_protocol(str(record.get("protocol") or "openai")).usage_fields(usage)
                if usage
                else {}
            )
            reads.append(int(raw.get("cache_read_input_tokens") or 0))
        return max(reads) if reads else None

    def normalize(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        first_success_sequence = None
        for record in records:
            if record.get("test_name") == "cache" and is_success(record):
                seq = (record.get("metadata") or {}).get("sequence")
                if seq is not None:
                    first_success_sequence = int(seq)
                    break
        cold_baseline = self._cold_baseline_cache_read(records, first_success_sequence)

        rows: list[dict[str, Any]] = []
        for record in records:
            protocol_name = str(record.get("protocol") or "openai")
            protocol = get_protocol(protocol_name)
            usage = record.get("usage") or {}
            if usage:
                raw_fields = protocol.usage_fields(usage)
            else:
                raw_fields = {
                    "input_tokens_raw": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0, "thinking_tokens": 0, "cache_field_path": None,
                }
            metadata = record.get("metadata") or {}
            success = is_success(record)
            is_cache_test = record.get("test_name") == "cache"
            is_stream = bool(record.get("raw_sse_events") is not None or record.get("stream"))
            phase = metadata.get("phase")
            cache_block = normalize_cache_fields(
                raw_fields,
                self.provider.semantics_for(protocol_name),
                is_cache_test=is_cache_test,
                sequence=metadata.get("sequence"),
                first_success_sequence=first_success_sequence,
                cache_expected=is_cache_test,
                cold_baseline_cache_read=cold_baseline,
                phase=phase,
            )
            status = record.get("http_status")
            row = NormalizedRequest(
                schema_version=SCHEMA_VERSION,
                request_id=str(record.get("request_id")),
                test_name=str(record.get("test_name")),
                suite=str(record.get("suite") or TEST_SUITE.get(str(record.get("test_name")), "unknown")),
                protocol=protocol_name,
                provider=str(record.get("provider") or self.provider.id),
                model=str(record.get("model") or self.model),
                started_at=record.get("started_at"),
                completed_at=record.get("completed_at"),
                success=success,
                http_status=status if isinstance(status, int) else None,
                error=record.get("error"),
                input_size_chars=metadata.get("input_size_chars"),
                concurrency=metadata.get("concurrency"),
                reasoning_effort=metadata.get("reasoning_effort"),
                sequence=metadata.get("sequence"),
                phase=phase,
                stream=is_stream,
                request_started_at=record.get("request_started_at") or record.get("started_at"),
                first_byte_ms=record.get("headers_ms"),
                first_event_ms=record.get("first_event_ms"),
                first_reasoning_ms=record.get("first_reasoning_ms"),
                first_content_ms=record.get("first_content_ms"),
                ttft_ms=record.get("ttft_ms"),
                total_ms=record.get("total_ms"),
                ttft_source=record.get("ttft_source"),
                ttft_valid=bool(record.get("ttft_valid")),
                ttft_missing_reason=record.get("ttft_missing_reason"),
                output_tokens=raw_fields.get("output_tokens"),
                thinking_tokens=raw_fields.get("thinking_tokens"),
                reasoning_chars=int(record.get("reasoning_chars") or 0),
                content_chars=int(record.get("content_chars") or 0),
                **cache_block,
            )
            rows.append(row.to_dict())
        return rows

    # ---------------------------------------------------------------- public
    def run(self, output_root: Path, api_key: str, *, layout: str = "nested") -> tuple[dict[str, Any], RunPaths]:
        paths = make_run_paths(output_root, self.provider.id, self.model, layout=layout)
        store = RunStore(paths)
        manifest = {
            "tool_version": TOOL_VERSION,
            "provider": self.provider.id,
            "provider_display_name": self.provider.display_name,
            "base_url": self.provider.base_url,
            "model": self.model,
            "profile": "inline",
            "protocols": self.config.protocols,
            "started_at": now_iso(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "params": self.config.to_dict()["profile"],
            "network_requests_executed": isinstance(self.transport, UrllibTransport),
            "secrets_persisted": False,
        }
        store.write_manifest(manifest)
        print(f"结果目录：{paths.root}", flush=True)

        suites = self.profile.suites
        if "smoke" in suites:
            print("[suite] smoke", flush=True)
            self.run_smoke(store, api_key)
        needs_stream = "reasoning" in suites or "latency" in suites
        preflight_ok = True
        if needs_stream:
            preflight_ok = self._stream_preflight_ok(store, api_key)
            if not preflight_ok:
                print("[suite] OpenAI SSE 预检失败，reasoning/latency 将跳过", flush=True)
        if "reasoning" in suites and preflight_ok:
            print("[suite] reasoning", flush=True)
            self.run_reasoning(store, api_key)
        if "latency" in suites and preflight_ok:
            print("[suite] latency", flush=True)
            for size in self.profile.input_sizes:
                prompt = self._latency_prompt(size)
                for concurrency in self.profile.concurrency_levels:
                    count = max(self.profile.samples_per_group, concurrency)
                    self.run_latency_group(store, api_key, prompt, size, concurrency, count)
        if "cache" in suites:
            print("[suite] cache", flush=True)
            self.run_cache(store, api_key)
        if "video" in suites:
            if "openai" in self.config.protocols:
                print("[suite] video", flush=True)
                self.run_video(store, api_key)
            else:
                print("[suite] video 跳过：需要 openai 协议", flush=True)

        rows = self.normalize(store.records)
        summary = self._finalize(store, rows, manifest)
        return summary, paths

    def _finalize(self, store: RunStore, rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
        from llm_bench.reporters import build_summary, render_markdown, write_requests_csv, write_requests_json

        write_requests_json(store.paths.normalized / "requests.json", rows)
        write_requests_csv(store.paths.normalized / "requests.csv", rows)
        success = sum(1 for r in rows if r.get("success"))
        manifest.update(
            completed_at=now_iso(),
            request_count=len(rows),
            success_count=success,
            failure_count=len(rows) - success,
        )
        summary = build_summary(rows, manifest)
        (store.paths.normalized / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (store.paths.report / "benchmark_report.md").write_text(render_markdown(summary), encoding="utf-8")
        store.write_manifest(manifest)
        print(f"完成。报告：{store.paths.report / 'benchmark_report.md'}", flush=True)
        return summary
