"""P1-4: TTFT summary must only cover TTFT-eligible requests.

Non-streaming requests are out of scope (not "missing"), preflight never
enters the statistics, streaming 2xx-without-delta does count as missing, the
core TTFT prefers the latency suite, and the global error rate / TTFT error
rate are reported separately.
"""
import unittest

try:
    from tests.helpers import make_row
except ImportError:  # pragma: no cover
    from helpers import make_row  # type: ignore

from llm_bench.metrics.latency import is_ttft_eligible
from llm_bench.reporters import build_summary, render_markdown

MANIFEST = {
    "tool_version": "0.1.0",
    "provider": "tostco",
    "model": "kimi-k3",
    "network_requests_executed": False,
    "secrets_persisted": False,
}


def latency_row(request_id: str, *, ttft_ms: float | None, valid: bool, size: int = 1024,
                conc: int = 1, success: bool = True,
                reason: str | None = None) -> dict:
    return make_row(
        request_id=request_id, test_name="latency", suite="latency", protocol="openai",
        sequence=1, input_size_chars=size, concurrency=conc, stream=True,
        ttft_ms=ttft_ms, ttft_valid=valid, ttft_source="content" if valid else None,
        ttft_missing_reason=reason, success=success,
        http_status=200 if success else 500,
        error=None if success else "HTTPError: 500",
        total_ms=800.0 if success else 50.0,
        cache_read_input_tokens=0, input_token_semantics="includes_cache",
    )


class TestTtftEligibility(unittest.TestCase):
    def test_smoke_non_streaming_not_missing(self):
        rows = [
            latency_row("lat_ok", ttft_ms=120.0, valid=True),
            make_row(request_id="smoke_1", test_name="smoke_chat", suite="smoke",
                     protocol="openai", stream=False, ttft_valid=False,
                     ttft_missing_reason="non_streaming_request"),
            make_row(request_id="smoke_2", test_name="smoke_anthropic", suite="smoke",
                     protocol="anthropic", stream=False, ttft_valid=False,
                     ttft_missing_reason="non_streaming_request"),
        ]
        summary = build_summary(rows, MANIFEST)
        ttft = summary["ttft"]
        self.assertEqual(ttft["attempted"], 1)
        self.assertEqual(ttft["valid_samples"], 1)
        self.assertEqual(ttft["missing_samples"], 0)
        self.assertNotIn("non_streaming_request", ttft["missing_reasons"])

    def test_non_streaming_cache_not_missing(self):
        # cache.stream=false: probes have no TTFT by design -> out of scope
        rows = [
            latency_row("lat_ok", ttft_ms=120.0, valid=True),
            make_row(request_id="cache_1", test_name="cache", suite="cache",
                     protocol="anthropic", stream=False, ttft_valid=False,
                     ttft_missing_reason="non_streaming_request"),
        ]
        summary = build_summary(rows, MANIFEST)
        self.assertFalse(is_ttft_eligible(rows[1]))
        self.assertEqual(summary["ttft"]["missing_samples"], 0)

    def test_streamed_2xx_without_delta_is_missing(self):
        rows = [
            latency_row("lat_ok", ttft_ms=120.0, valid=True),
            latency_row("lat_silent", ttft_ms=None, valid=False, reason="no_semantic_delta"),
            latency_row("lat_http_error", ttft_ms=None, valid=False, success=False,
                        reason="http_error"),
        ]
        summary = build_summary(rows, MANIFEST)
        ttft = summary["ttft"]
        self.assertEqual(ttft["attempted"], 3)
        self.assertEqual(ttft["valid_samples"], 1)
        self.assertEqual(ttft["missing_samples"], 2)
        self.assertEqual(ttft["missing_reasons"].get("no_semantic_delta"), 1)
        self.assertEqual(ttft["missing_reasons"].get("http_error"), 1)
        self.assertTrue(any("TTFT 缺失" in w and "no_semantic_delta" in w for w in summary["warnings"]))
        self.assertTrue(any("TTFT 缺失" in w and "http_error" in w for w in summary["warnings"]))

    def test_preflight_never_enters_ttft_or_global_error_rate(self):
        rows = [
            make_row(request_id="pre_1", test_name="stream_preflight", suite="preflight",
                     protocol="openai", stream=True, success=False, http_status=500,
                     error="HTTPError: 500", ttft_valid=False,
                     ttft_missing_reason="http_error"),
            latency_row("lat_ok", ttft_ms=120.0, valid=True),
        ]
        summary = build_summary(rows, MANIFEST)
        ttft = summary["ttft"]
        self.assertEqual(ttft["attempted"], 1)
        self.assertEqual(ttft["missing_samples"], 0)
        core = summary["core"]
        self.assertEqual(core["error_rate"], 0.0)
        self.assertEqual(core["global_error_rate"], 0.0)
        self.assertEqual(core["ttft_error_rate"], 0.0)
        # preflight still shows up in the raw failure list, but not in rates
        self.assertEqual(len(summary["failures"]), 1)


class TestCoreTtftSource(unittest.TestCase):
    def test_core_prefers_latency_suite(self):
        rows = [
            latency_row("lat_1", ttft_ms=100.0, valid=True),
            latency_row("lat_2", ttft_ms=200.0, valid=True, size=8192, conc=4),
            make_row(request_id="reas_1", test_name="reasoning_effort", suite="reasoning",
                     protocol="openai", stream=True, ttft_ms=900.0, ttft_valid=True,
                     ttft_source="reasoning", reasoning_effort="high", total_ms=2000.0,
                     cache_read_input_tokens=0, input_token_semantics="includes_cache"),
        ]
        summary = build_summary(rows, MANIFEST)
        core = summary["core"]
        self.assertEqual(core["ttft_scope"], "latency")
        self.assertEqual(core["ttft_p50_ms"], 100.0)
        self.assertEqual(core["ttft_p95_ms"], 200.0)
        self.assertIn("latency", summary["ttft"]["by_suite"])
        self.assertIn("reasoning", summary["ttft"]["by_suite"])

    def test_core_falls_back_to_all_eligible(self):
        rows = [
            make_row(request_id="reas_1", test_name="reasoning_effort", suite="reasoning",
                     protocol="openai", stream=True, ttft_ms=900.0, ttft_valid=True,
                     ttft_source="reasoning", reasoning_effort="high", total_ms=2000.0,
                     cache_read_input_tokens=0, input_token_semantics="includes_cache"),
        ]
        summary = build_summary(rows, MANIFEST)
        self.assertEqual(summary["core"]["ttft_scope"], "eligible_all")
        self.assertEqual(summary["core"]["ttft_p50_ms"], 900.0)


class TestErrorRates(unittest.TestCase):
    def test_global_and_ttft_error_rates_are_separate(self):
        rows = [
            latency_row(f"lat_{i}", ttft_ms=100.0, valid=True) for i in range(5)
        ]
        rows.append(latency_row("lat_fail", ttft_ms=None, valid=False, success=False,
                                reason="http_error"))
        rows.append(make_row(request_id="smoke_fail", test_name="smoke_chat", suite="smoke",
                             protocol="openai", stream=False, success=False,
                             http_status=429, error="HTTPError: 429"))
        rows.append(make_row(request_id="pre_fail", test_name="stream_preflight",
                             suite="preflight", protocol="openai", stream=True,
                             success=False, http_status=500, error="HTTPError: 500",
                             ttft_valid=False, ttft_missing_reason="http_error"))
        summary = build_summary(rows, MANIFEST)
        core = summary["core"]
        # formal requests: 6 latency + 1 smoke (preflight excluded) -> 2/7
        self.assertAlmostEqual(core["global_error_rate"], 2 / 7, places=6)
        # TTFT-eligible: 6 latency (1 failed) -> 1/6
        self.assertAlmostEqual(core["ttft_error_rate"], 1 / 6, places=6)


class TestGroupings(unittest.TestCase):
    def test_no_none_none_groups(self):
        rows = [
            latency_row("lat_1", ttft_ms=100.0, valid=True),
            latency_row("lat_2", ttft_ms=200.0, valid=True, size=8192, conc=4),
            # eligible but without input_size/concurrency -> must be excluded
            # from groups rather than forming a None|None bucket
            make_row(request_id="cache_1", test_name="cache", suite="cache",
                     protocol="anthropic", stream=True, ttft_ms=300.0, ttft_valid=True,
                     ttft_source="content", total_ms=900.0),
        ]
        summary = build_summary(rows, MANIFEST)
        groups = summary["ttft"]["groups"]
        self.assertEqual(len(groups), 2)
        for group in groups:
            for value in group["group"].values():
                self.assertIsNotNone(value)
        markdown = render_markdown(summary)
        self.assertIn("|1024|1|", markdown)
        self.assertIn("|8192|4|", markdown)
        self.assertNotIn("|None|None|", markdown)


if __name__ == "__main__":
    unittest.main()
