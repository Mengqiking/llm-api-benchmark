"""Offline execution tests: the full Runner flow with FakeTransport, plus CLI
dry-run/report commands. No test in this file may touch the network."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from llm_bench.config import load_profile, load_provider, resolve
from llm_bench.runner import Runner
from llm_bench.transport.http import FakeResponse, FakeTransport

TOOLKIT = Path(__file__).resolve().parents[1]
PROVIDERS = TOOLKIT / "providers"
PROFILES = TOOLKIT / "profiles"

SSE_OK = [
    (10.0, 'data: {"choices": [{"delta": {"role": "assistant"}, "index": 0}]}'),
    (120.0, 'data: {"choices": [{"delta": {"content": "OK"}, "index": 0}]}'),
    (200.0, 'data: {"choices": [{"delta": {}, "index": 0}], "usage": {"prompt_tokens": 20, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 0}}}'),
    (210.0, "data: [DONE]"),
]

JSON_CHAT = json.dumps({
    "id": "chatcmpl-1",
    "usage": {"prompt_tokens": 20, "completion_tokens": 1,
              "prompt_tokens_details": {"cached_tokens": 0}},
})

ANTHROPIC_USAGE = {
    "input_tokens": 1705, "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 64, "output_tokens": 8,
}


def sse_data(payload: dict, ms: float) -> tuple[float, str]:
    return (ms, f"data: {json.dumps(payload)}")


def anthropic_cache_stream(read: int, with_usage: bool = True) -> list[tuple[float, str]]:
    """Anthropic-style streamed cache probe: usage rides message_start
    (input side, incl. cache fields) and message_delta (output side)."""
    usage = {
        "input_tokens": 1705, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": read, "output_tokens": 1,
    } if with_usage else {}
    events = [
        sse_data({"type": "message_start", "message": {"usage": usage}}, 100.0),
        sse_data({"type": "content_block_start", "index": 0}, 150.0),
        sse_data({"type": "content_block_delta",
                  "delta": {"type": "text_delta", "text": "OK"}}, 300.0),
        sse_data({"type": "message_delta", "usage": {"output_tokens": 8}}, 400.0),
        sse_data({"type": "message_stop"}, 420.0),
    ]
    return events


def make_selector(cache_reads=None, cache_with_usage=True):
    """Stateful selector: cache reads are 64 for the first call then 1705.

    Matching is prefix-based because the transport sees the full request id
    (`<test_name>_<uuid>`), and test names themselves contain underscores.
    Cache probes are streamed (anthropic protocol for tostco/tokenstars).
    """
    state = {"cache_calls": 0}
    cache_reads = cache_reads or [64, 1705, 1705, 1705, 1705]

    def selector(test_name, method, url):
        state["cache_calls"] += 1 if test_name.startswith("cache") else 0
        if method == "GET":
            return FakeResponse(body=json.dumps({"data": []}))
        if test_name.startswith(("smoke_chat", "smoke_anthropic", "thinking_disable")):
            usage = ANTHROPIC_USAGE if not test_name.startswith("smoke_chat") else json.loads(JSON_CHAT)["usage"]
            return FakeResponse(body=json.dumps({"id": "x", "usage": usage}))
        if test_name.startswith("cache"):
            read = cache_reads[min(state["cache_calls"], len(cache_reads)) - 1]
            return FakeResponse(
                sse_events=anthropic_cache_stream(read, with_usage=cache_with_usage),
                total_ms=450.0,
            )
        if test_name.startswith(("stream_preflight", "reasoning_effort", "latency")):
            return FakeResponse(sse_events=SSE_OK, total_ms=250.0)
        return FakeResponse(body="{}")

    return selector


def build_resolved(provider_id="tostco", profile_id="standard"):
    provider = load_provider(provider_id, PROVIDERS)
    profile = load_profile(profile_id, PROFILES)
    return resolve(provider, None, profile)


def block_network():
    return mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))


class TestRunnerOffline(unittest.TestCase):
    def test_full_smoke_profile_without_network(self):
        resolved = build_resolved(profile_id="smoke")
        with block_network():
            with tempfile.TemporaryDirectory() as tmp:
                runner = Runner(resolved, FakeTransport(selector=make_selector()), run_id="unittest")
                summary, paths = runner.run(Path(tmp), "test-key-not-real")
                # the API key must not appear anywhere in persisted output
                for file in paths.root.rglob("*"):
                    if file.is_file():
                        self.assertNotIn("test-key-not-real", file.read_text(encoding="utf-8", errors="ignore"))
                manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
                self.assertFalse(manifest["network_requests_executed"])
                self.assertTrue((paths.normalized / "requests.json").is_file())
                self.assertTrue((paths.normalized / "requests.csv").is_file())
                self.assertTrue((paths.report / "benchmark_report.md").is_file())
                core = summary["core"]
                self.assertEqual(core["requests"], core["success"])
                report = (paths.report / "benchmark_report.md").read_text(encoding="utf-8")
                self.assertIn("## 核心性能摘要", report)
                # smoke profile has no cache suite -> explicit N/A note instead
                self.assertIn("本次运行未执行缓存测试", report)

    def test_cache_suite_flow(self):
        # tostco declares anthropic semantics unknown -> token rates are N/A,
        # never a guessed percentage (P1-1)
        resolved = build_resolved(profile_id="cache")
        with block_network():
            with tempfile.TemporaryDirectory() as tmp:
                runner = Runner(resolved, FakeTransport(selector=make_selector()), run_id="unittest2")
                summary, _ = runner.run(Path(tmp), "test-key-not-real")
        cache = summary["cache"]
        self.assertEqual(cache["attempted"], 5)
        self.assertEqual(cache["successful"], 5)
        self.assertEqual(cache["cold_start"]["sequence"], 1)
        self.assertEqual(cache["post_cold"]["attempted"], 4)
        self.assertIsNone(cache["token_hit_rate"])
        self.assertIsNone(cache["post_cold"]["token_hit_rate"])
        self.assertEqual(cache["input_token_semantics"], "unknown")
        self.assertEqual(cache["countable"], 0)
        self.assertTrue(any("可统计" in w or "语义" in w for w in cache["warnings"]))

    def test_cache_suite_flow_tokenstars_full_metrics(self):
        # tokenstars declares anthropic=excludes_cache -> full rates computed
        resolved = build_resolved(provider_id="tokenstars", profile_id="cache")
        with block_network():
            with tempfile.TemporaryDirectory() as tmp:
                runner = Runner(resolved, FakeTransport(selector=make_selector()), run_id="unittest4")
                summary, _ = runner.run(Path(tmp), "test-key-not-real")
        cache = summary["cache"]
        self.assertEqual(cache["input_token_semantics"], "excludes_cache")
        self.assertEqual(cache["countable"], 5)
        # excludes_cache: total = input + creation + read
        self.assertAlmostEqual(
            cache["token_hit_rate"], (64 + 1705 * 4) / (1769 + 3410 * 4), places=6
        )
        self.assertAlmostEqual(cache["post_cold"]["token_hit_rate"], 1705 / 3410, places=6)
        self.assertLessEqual(cache["token_hit_rate"], 1.0)
        self.assertEqual(cache["post_cold"]["request_hits"], 4)

    def test_cache_stream_captures_ttft_and_usage(self):
        # P1-2: streamed cache probes must yield TTFT *and* cache counters
        resolved = build_resolved(profile_id="cache")
        with block_network():
            with tempfile.TemporaryDirectory() as tmp:
                runner = Runner(resolved, FakeTransport(selector=make_selector()), run_id="unittest3")
                summary, paths = runner.run(Path(tmp), "test-key-not-real")
                rows = json.loads(
                    (paths.normalized / "requests.json").read_text(encoding="utf-8")
                )
                cache_rows = [r for r in rows if r["suite"] == "cache"]
                self.assertEqual(len(cache_rows), 5)
                for row in cache_rows:
                    self.assertTrue(row["stream"])
                    self.assertTrue(row["ttft_valid"])
                    self.assertEqual(row["ttft_ms"], 300.0)
                    self.assertEqual(row["ttft_source"], "content")
                    self.assertIsNotNone(row["total_ms"])
                # the persisted request body really asked for streaming
                raw = json.loads(
                    (paths.raw_requests / f"{cache_rows[0]['request_id']}.json").read_text(encoding="utf-8")
                )
                self.assertTrue(raw["request_body"]["stream"])
        cache = summary["cache"]
        # cold baseline 64; warm reads 1705 exceed it -> target-prefix evidence
        self.assertEqual(cache["cold_baseline_cache_read_tokens"], 64)
        self.assertEqual(cache["post_cold"]["request_hits"], 4)
        self.assertEqual(cache["post_cold"]["request_hit_rate"], 1.0)
        self.assertEqual(cache["post_cold"]["target_prefix_increment_tokens"], (1705 - 64) * 4)
        scopes = {r["sequence"]: r["cache_hit_scope"] for r in cache["requests"]}
        self.assertEqual(scopes[1], "any")
        for seq in (2, 3, 4, 5):
            self.assertEqual(scopes[seq], "target_prefix")
        # real hit/miss TTFT numbers are now available
        self.assertEqual(cache["hit_ttft"]["ttft_p50_ms"], 300.0)
        self.assertEqual(cache["hit_ttft"]["total_p50_ms"], 450.0)

    def test_cache_stream_without_usage_not_countable(self):
        # P1-2: streamed response without usage -> cache metrics cannot be
        # computed, nothing is fabricated and a warning is emitted
        resolved = build_resolved(profile_id="cache")
        with block_network():
            with tempfile.TemporaryDirectory() as tmp:
                runner = Runner(
                    resolved,
                    FakeTransport(selector=make_selector(cache_with_usage=False)),
                    run_id="unittest5",
                )
                summary, _ = runner.run(Path(tmp), "test-key-not-real")
        cache = summary["cache"]
        self.assertEqual(cache["successful"], 5)
        self.assertEqual(cache["countable"], 0)
        self.assertIsNone(cache["token_hit_rate"])
        self.assertEqual(cache["cache_read_input_tokens"], 0)
        self.assertEqual(cache["request_hits"], 0)
        self.assertTrue(any("缓存字段" in w or "可统计" in w for w in cache["warnings"]))
        # TTFT itself is still measured from the stream (all rows count as misses)
        self.assertEqual(cache["miss_ttft"]["ttft_p50_ms"], 300.0)

    def test_cache_stream_false_allows_non_streaming(self):
        # P1-2: cache.stream=false is honoured; TTFT is then explicitly
        # unavailable (non_streaming_request), cache metrics still work
        from dataclasses import replace

        resolved = build_resolved(provider_id="tokenstars", profile_id="cache")
        profile = replace(resolved.profile, cache=replace(resolved.profile.cache, stream=False))
        resolved = replace(resolved, profile=profile)
        reads = iter([64, 1705, 1705, 1705, 1705])

        def selector(test_name, method, url):
            if method == "GET":
                return FakeResponse(body=json.dumps({"data": []}))
            if test_name.startswith("cache"):
                return FakeResponse(body=json.dumps({
                    "id": "x",
                    "usage": {
                        "input_tokens": 1705, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": next(reads, 1705),
                    },
                }))
            return FakeResponse(body="{}")

        with block_network():
            with tempfile.TemporaryDirectory() as tmp:
                runner = Runner(resolved, FakeTransport(selector=selector), run_id="unittest6")
                summary, paths = runner.run(Path(tmp), "test-key-not-real")
                rows = json.loads((paths.normalized / "requests.json").read_text(encoding="utf-8"))
        cache_rows = [r for r in rows if r["suite"] == "cache"]
        for row in cache_rows:
            self.assertFalse(row["stream"])
            self.assertFalse(row["ttft_valid"])
            self.assertEqual(row["ttft_missing_reason"], "non_streaming_request")
        cache = summary["cache"]
        self.assertIsNone(cache["hit_ttft"]["ttft_p50_ms"])
        # usage still comes from the JSON body, so cache stats remain valid
        self.assertEqual(cache["countable"], 5)
        self.assertEqual(cache["post_cold"]["request_hits"], 4)

    def test_openai_streaming_cache_captures_ttft_and_cached_tokens(self):
        # P1-2: openai_observation mode streams and reads cached_tokens from
        # the usage tail event (stream_options.include_usage)
        from dataclasses import replace

        resolved = build_resolved(profile_id="cache")
        profile = replace(
            resolved.profile, cache=replace(resolved.profile.cache, mode="openai_observation")
        )
        resolved = replace(resolved, profile=profile)
        calls = {"n": 0}

        def selector(test_name, method, url):
            calls["n"] += 1 if test_name.startswith("cache") else 0
            if method == "GET":
                return FakeResponse(body=json.dumps({"data": []}))
            if test_name.startswith("cache"):
                cached = 0 if calls["n"] == 1 else 1600
                usage = {
                    "prompt_tokens": 1705, "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": cached},
                }
                events = [
                    (100.0, 'data: {"choices": [{"delta": {}, "index": 0}]}'),
                    (300.0, 'data: {"choices": [{"delta": {"content": "OK"}, "index": 0}]}'),
                    (400.0, "data: " + json.dumps({"choices": [], "usage": usage})),
                    (420.0, "data: [DONE]"),
                ]
                return FakeResponse(sse_events=events, total_ms=450.0)
            return FakeResponse(body="{}")

        with block_network():
            with tempfile.TemporaryDirectory() as tmp:
                runner = Runner(resolved, FakeTransport(selector=selector), run_id="unittest7")
                summary, paths = runner.run(Path(tmp), "test-key-not-real")
                raw = json.loads(
                    next((paths.raw_requests).glob("cache_*.json")).read_text(encoding="utf-8")
                )
        self.assertTrue(raw["request_body"]["stream"])
        self.assertTrue(raw["request_body"]["stream_options"]["include_usage"])
        cache = summary["cache"]
        self.assertEqual(cache["input_token_semantics"], "includes_cache")
        self.assertEqual(cache["cache_metric_source"], "usage.prompt_tokens_details.cached_tokens")
        self.assertEqual(cache["cold_baseline_cache_read_tokens"], 0)
        self.assertEqual(cache["post_cold"]["request_hits"], 4)
        self.assertAlmostEqual(cache["post_cold"]["token_hit_rate"], 1600 / 1705, places=6)
        rows = cache["requests"]
        self.assertTrue(all(r["ttft_valid"] and r["ttft_ms"] == 300.0 for r in rows))

    def test_unique_cache_prefix_per_run(self):
        resolved = build_resolved(profile_id="cache")
        prefixes = set()
        with tempfile.TemporaryDirectory() as tmp:
            for run_id in ("runA", "runB"):
                runner = Runner(resolved, FakeTransport(selector=make_selector()), run_id=run_id)
                prefix = runner._cache_prefix()
                prefixes.add(prefix)
                self.assertIn(run_id, prefix)
        self.assertEqual(len(prefixes), 2)


class TestCli(unittest.TestCase):
    def _providers_args(self):
        return [
            "--providers-dir", str(PROVIDERS),
            "--profiles-dir", str(PROFILES),
        ]

    def test_dry_run_all_providers_no_network(self):
        from llm_bench.cli import main as cli_main

        for provider_id in ("tostco", "tokenstars", "openjuly"):
            with block_network():
                buffer_out, buffer_err = io.StringIO(), io.StringIO()
                with redirect_stdout(buffer_out), redirect_stderr(buffer_err):
                    code = cli_main([
                        "dry-run", "--provider", provider_id, "--model", "kimi-k3",
                        "--profile", "standard", *self._providers_args(),
                    ])
                self.assertEqual(code, 0)
                payload = json.loads(buffer_out.getvalue())
                self.assertFalse(payload["network_requests_executed"])
                self.assertEqual(payload["model"], "kimi-k3")
                self.assertIn("cold_start_requests", json.dumps(payload["profile"]))
                self.assertNotIn("api_key", json.dumps(payload).replace("api_key_env", ""))

    def test_dry_run_unknown_provider_exit_2(self):
        from llm_bench.cli import main as cli_main

        with redirect_stderr(io.StringIO()):
            code = cli_main([
                "dry-run", "--provider", "ghost", "--profile", "standard",
                *self._providers_args(),
            ])
        self.assertEqual(code, 2)

    def test_run_without_api_key_exit_2(self):
        from llm_bench.cli import main as cli_main

        env = {"TOSTCO_API_KEY": ""}
        with mock.patch.dict("os.environ", env, clear=False):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                with block_network():
                    code = cli_main([
                        "run", "--provider", "tostco", "--profile", "smoke",
                        *self._providers_args(), "--output", self._tmpdir(),
                    ])
        self.assertEqual(code, 2)

    @staticmethod
    def _tmpdir():
        import tempfile

        return tempfile.mkdtemp(prefix="llmbench_cli_")

    def test_report_command_offline(self):
        from llm_bench.cli import main as cli_main
        from llm_bench.legacy import reanalyze_run

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "re"
            with block_network():
                _summary, run_dir = reanalyze_run(TOOLKIT / "tests" / "fixtures" / "historical_tostco", out)
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = cli_main(["report", "--run", str(run_dir), "--output", str(Path(tmp) / "rep")])
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["network_requests_executed"])
            report = Path(payload["output"]).read_text(encoding="utf-8")
            self.assertIn("## 核心性能摘要", report)

    def test_report_requires_normalized(self):
        from llm_bench.cli import main as cli_main

        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stderr(io.StringIO()):
                code = cli_main(["report", "--run", tmp])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
