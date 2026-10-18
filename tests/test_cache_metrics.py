import unittest

try:
    from tests.helpers import make_row
except ImportError:  # pragma: no cover
    from helpers import make_row  # type: ignore

from llm_bench.metrics.cache import (
    determine_hit_scope,
    normalize_cache_fields,
    safe_rate,
    summarize_cache,
)


class TestSafeRate(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(safe_rate(50, 200), (0.25, None))

    def test_over_100_percent_rejected(self):
        rate, warning = safe_rate(150, 100)
        self.assertIsNone(rate)
        self.assertIn("超过 100%", warning)

    def test_zero_denominator(self):
        rate, warning = safe_rate(10, 0)
        self.assertIsNone(rate)
        self.assertIsNone(warning)


class TestNormalizeAnthropicExcludesCache(unittest.TestCase):
    def test_total_adds_cache_tokens(self):
        raw = {
            "input_tokens_raw": 1705, "output_tokens": 8,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 64,
            "thinking_tokens": 0, "cache_field_path": "usage.cache_read_input_tokens",
        }
        block = normalize_cache_fields(raw, "excludes_cache", is_cache_test=True,
                                       sequence=1, first_success_sequence=1)
        self.assertEqual(block["input_tokens_raw"], 1705)
        self.assertEqual(block["uncached_input_tokens"], 1705)
        self.assertEqual(block["total_logical_input_tokens"], 1769)
        self.assertAlmostEqual(block["cache_read_input_tokens"] / block["total_logical_input_tokens"],
                               64 / 1769)
        self.assertEqual(block["input_token_semantics"], "excludes_cache")
        self.assertTrue(block["cache_metrics_valid"])
        self.assertTrue(block["cache_hit"])
        self.assertEqual(block["cache_hit_scope"], "any")

    def test_with_creation(self):
        raw = {
            "input_tokens_raw": 100, "output_tokens": 1,
            "cache_creation_input_tokens": 500, "cache_read_input_tokens": 200,
            "thinking_tokens": 0, "cache_field_path": "usage.cache_read_input_tokens",
        }
        block = normalize_cache_fields(raw, "excludes_cache", is_cache_test=True,
                                       sequence=2, first_success_sequence=1, cold_baseline_cache_read=64)
        self.assertEqual(block["total_logical_input_tokens"], 800)
        self.assertEqual(block["cache_hit_scope"], "target_prefix")
        self.assertTrue(block["target_prefix_evidence"])
        self.assertEqual(block["target_prefix_cache_read_tokens"], 136)
        self.assertEqual(block["cold_baseline_cache_read_tokens"], 64)


class TestNormalizeOpenAiIncludesCache(unittest.TestCase):
    def test_total_is_prompt_tokens(self):
        raw = {
            "input_tokens_raw": 1000, "output_tokens": 5,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 800,
            "thinking_tokens": 0, "cache_field_path": "usage.prompt_tokens_details.cached_tokens",
        }
        block = normalize_cache_fields(raw, "includes_cache", is_cache_test=True,
                                       sequence=2, first_success_sequence=1)
        self.assertEqual(block["total_logical_input_tokens"], 1000)
        self.assertEqual(block["uncached_input_tokens"], 200)
        self.assertAlmostEqual(0.8, block["cache_read_input_tokens"] / block["total_logical_input_tokens"])
        self.assertTrue(block["cache_metrics_valid"])

    def test_inconsistent_input_flags_warning(self):
        raw = {
            "input_tokens_raw": 100, "output_tokens": 1,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 150,
            "thinking_tokens": 0, "cache_field_path": "usage.prompt_tokens_details.cached_tokens",
        }
        block = normalize_cache_fields(raw, "includes_cache", is_cache_test=True,
                                       sequence=2, first_success_sequence=1)
        self.assertEqual(block["total_logical_input_tokens"], 100)
        self.assertFalse(block["cache_metrics_valid"])
        self.assertIn("可能不一致", block["cache_metrics_warning"])


class TestUnknownSemantics(unittest.TestCase):
    def test_no_pseudo_rate(self):
        raw = {
            "input_tokens_raw": 100, "output_tokens": 1,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 40,
            "thinking_tokens": 0, "cache_field_path": "unknown",
        }
        block = normalize_cache_fields(raw, "unknown", is_cache_test=True,
                                       sequence=2, first_success_sequence=1)
        self.assertIsNone(block["total_logical_input_tokens"])
        self.assertIsNone(block["uncached_input_tokens"])
        self.assertFalse(block["cache_metrics_valid"])
        self.assertIn("未知", block["cache_metrics_warning"])
        # raw fields are preserved
        self.assertEqual(block["cache_read_input_tokens"], 40)
        self.assertEqual(block["input_tokens_raw"], 100)


class TestHitScope(unittest.TestCase):
    def test_cold_start_is_any(self):
        self.assertEqual(determine_hit_scope(cache_read=64, is_cache_test=True,
                                             sequence=1, first_success_sequence=1), "any")

    def test_warm_is_target_prefix(self):
        self.assertEqual(determine_hit_scope(cache_read=64, is_cache_test=True,
                                             sequence=3, first_success_sequence=1,
                                             cold_baseline_cache_read=0), "target_prefix")

    def test_no_read_is_none(self):
        self.assertEqual(determine_hit_scope(cache_read=0, is_cache_test=True,
                                             sequence=3, first_success_sequence=1), "none")

    def test_unknown_cases(self):
        self.assertEqual(determine_hit_scope(cache_read=10, is_cache_test=False,
                                             sequence=1, first_success_sequence=1), "unknown")
        self.assertEqual(determine_hit_scope(cache_read=10, is_cache_test=True,
                                             sequence=None, first_success_sequence=1), "unknown")


class TestTargetPrefixBaseline(unittest.TestCase):
    """target_prefix requires warm reads to EXCEED the cold-start baseline."""

    def _warm(self, read, baseline):
        return determine_hit_scope(
            cache_read=read, is_cache_test=True, sequence=3,
            first_success_sequence=1, cold_baseline_cache_read=baseline,
        )

    def test_warm_not_exceeding_baseline_is_not_target_prefix(self):
        # cold=64, warm=64: identical read is NOT evidence of the target prefix
        self.assertEqual(self._warm(64, 64), "any")

    def test_warm_exceeding_baseline_is_target_prefix(self):
        self.assertEqual(self._warm(1664, 64), "target_prefix")

    def test_zero_baseline(self):
        self.assertEqual(self._warm(1664, 0), "target_prefix")

    def test_missing_baseline_is_unknown(self):
        self.assertEqual(self._warm(1664, None), "unknown")

    def test_normalize_records_increment(self):
        raw = {
            "input_tokens_raw": 1705, "output_tokens": 1,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1664,
            "thinking_tokens": 0, "cache_field_path": "x",
        }
        block = normalize_cache_fields(raw, "excludes_cache", is_cache_test=True,
                                       sequence=2, first_success_sequence=1, cold_baseline_cache_read=64)
        self.assertEqual(block["target_prefix_cache_read_tokens"], 1600)
        self.assertTrue(block["target_prefix_evidence"])

    def test_multiple_cold_requests_take_max_baseline(self):
        rows = [
            make_row(sequence=1, phase="cold_start", cache_read_input_tokens=32, ttft_ms=None,
                     ttft_valid=False, cache_hit_scope="any"),
            make_row(sequence=2, request_id="req_2", phase="cold_start",
                     cache_read_input_tokens=128, ttft_ms=None, ttft_valid=False,
                     cache_hit_scope="any"),
            make_row(sequence=3, request_id="req_3", phase="warm",
                     cache_read_input_tokens=128, ttft_ms=None, ttft_valid=False,
                     cache_hit_scope="any"),
            make_row(sequence=4, request_id="req_4", phase="warm",
                     cache_read_input_tokens=1705, ttft_ms=None, ttft_valid=False,
                     cache_hit_scope="target_prefix"),
        ]
        summary = summarize_cache(rows)
        self.assertEqual(summary["cold_baseline_cache_read_tokens"], 128)
        # warm seq3 read == baseline -> any; only seq4 beats the baseline
        self.assertEqual(summary["post_cold"]["request_hits"], 1)
        self.assertEqual(summary["post_cold"]["attempted"], 2)
        self.assertEqual(summary["target_prefix_cache_read_tokens"], 1705 - 128)


class TestSummarizeCache(unittest.TestCase):
    def _rows(self):
        # seq1 cold: read 64; seq2..4 warm: read 1705; one failed request seq5
        # (ttft_valid=True simulates streamed cache probes so hit/miss TTFT
        # comparison has data; plain Anthropic JSON probes would have no TTFT)
        rows = [
            make_row(sequence=1, input_tokens_raw=1705, cache_read_input_tokens=64,
                     total_logical_input_tokens=1769, ttft_ms=900.0, total_ms=1700.0,
                     cache_hit_scope="any", ttft_valid=True),
        ]
        for seq in (2, 3, 4):
            rows.append(make_row(
                sequence=seq, request_id=f"req_{seq}",
                input_tokens_raw=1705, cache_read_input_tokens=1705,
                total_logical_input_tokens=3410, ttft_ms=300.0, total_ms=900.0,
                cache_hit_scope="target_prefix", ttft_valid=True,
                ttft_missing_reason=None,
            ))
        rows.append(make_row(sequence=5, request_id="req_5", success=False, http_status=502,
                             input_tokens_raw=0, cache_read_input_tokens=0,
                             total_logical_input_tokens=0, cache_hit=False, ttft_ms=None,
                             ttft_valid=False, error="HTTPError: 502"))
        return rows

    def test_summary(self):
        summary = summarize_cache(self._rows())
        self.assertEqual(summary["attempted"], 5)
        self.assertEqual(summary["successful"], 4)
        self.assertEqual(summary["countable"], 4)
        self.assertEqual(summary["request_hits"], 4)
        self.assertAlmostEqual(summary["request_hit_rate"], 1.0)
        self.assertEqual(summary["post_cold"]["attempted"], 3)
        self.assertEqual(summary["post_cold"]["request_hits"], 3)
        self.assertAlmostEqual(summary["post_cold"]["request_hit_rate"], 1.0)
        # token weighted: (64 + 1705*3) / (1769 + 3410*3)
        expected = (64 + 1705 * 3) / (1769 + 3410 * 3)
        self.assertAlmostEqual(summary["token_hit_rate"], expected)
        self.assertAlmostEqual(summary["post_cold"]["token_hit_rate"], 1705 / 3410)
        self.assertEqual(summary["cache_creation_input_tokens"], 0)
        self.assertEqual(summary["cache_read_input_tokens"], 64 + 1705 * 3)
        self.assertEqual(summary["input_token_semantics"], "excludes_cache")
        # hit vs miss comparison
        self.assertEqual(summary["hit_ttft"]["ttft_p50_ms"], 300.0)
        self.assertEqual(summary["hit_ttft"]["ttft_p95_ms"], 900.0)
        self.assertEqual(summary["miss_ttft"]["ttft_p50_ms"], None)
        self.assertEqual(summary["hit_ttft"]["total_p50_ms"], 900.0)
        self.assertEqual(summary["cold_start"]["sequence"], 1)
        # target-prefix evidence via cold-start baseline
        self.assertEqual(summary["cold_baseline_cache_read_tokens"], 64)
        self.assertEqual(summary["post_cold"]["request_hits"], 3)
        self.assertAlmostEqual(summary["post_cold"]["request_hit_rate"], 1.0)
        self.assertEqual(summary["post_cold"]["target_prefix_increment_tokens"], (1705 - 64) * 3)
        self.assertEqual(summary["target_prefix_request_hits"], 3)

    def test_summary_empty(self):
        summary = summarize_cache([])
        self.assertEqual(summary["attempted"], 0)
        self.assertIsNone(summary["token_hit_rate"])

    def test_unknown_semantics_token_rates_na(self):
        rows = [
            make_row(sequence=1, input_token_semantics="unknown",
                     total_logical_input_tokens=None, uncached_input_tokens=None,
                     cache_metrics_valid=False,
                     cache_metrics_warning="input_token_semantics 未知，无法给出可信的缓存命中率"),
        ]
        for seq in (2, 3):
            rows.append(make_row(sequence=seq, request_id=f"req_{seq}",
                                 input_token_semantics="unknown",
                                 total_logical_input_tokens=None, uncached_input_tokens=None,
                                 cache_metrics_valid=False,
                                 cache_metrics_warning="input_token_semantics 未知，无法给出可信的缓存命中率"))
        summary = summarize_cache(rows)
        self.assertIsNone(summary["token_hit_rate"])
        self.assertIsNone(summary["post_cold"]["token_hit_rate"])
        self.assertEqual(summary["input_token_semantics"], "unknown")
        self.assertTrue(any("语义" in w for w in summary["warnings"]))

    def test_missing_baseline_produces_warning(self):
        rows = [
            make_row(sequence=2, request_id="req_2", phase="warm",
                     cache_read_input_tokens=1705, total_logical_input_tokens=3410,
                     ttft_ms=None, ttft_valid=False, cache_hit_scope="unknown"),
        ]
        summary = summarize_cache(rows)
        self.assertIsNone(summary["cold_baseline_cache_read_tokens"])
        self.assertTrue(any("缺少冷启动基线" in w for w in summary["warnings"]))
        self.assertEqual(summary["post_cold"]["request_hits"], 0)

    def test_warnings_propagate(self):
        rows = [make_row(sequence=1, cache_metrics_valid=False,
                         cache_metrics_warning="usage 中未发现任何缓存字段")]
        summary = summarize_cache(rows)
        self.assertTrue(any("未发现任何缓存字段" in w for w in summary["warnings"]))

    def test_rate_never_exceeds_100_percent(self):
        rows = [
            make_row(sequence=1, input_tokens_raw=100, cache_read_input_tokens=150,
                     total_logical_input_tokens=100, cache_metrics_valid=False,
                     cache_metrics_warning="缓存读取(150) 大于总逻辑输入(100)",
                     input_token_semantics="includes_cache"),
        ]
        summary = summarize_cache(rows)
        self.assertIsNone(summary["token_hit_rate"])
        self.assertTrue(summary["warnings"])
        for warning in summary["warnings"]:
            self.assertNotIn("150.00%", warning)


class TestCacheExpectedFields(unittest.TestCase):
    def test_cache_test_without_fields_is_invalid(self):
        raw = {
            "input_tokens_raw": 100, "output_tokens": 1,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "thinking_tokens": 0, "cache_field_path": None,
        }
        block = normalize_cache_fields(raw, "excludes_cache", is_cache_test=True,
                                       sequence=1, first_success_sequence=None, cache_expected=True)
        self.assertFalse(block["cache_metrics_valid"])
        self.assertIn("未发现任何缓存字段", block["cache_metrics_warning"])


if __name__ == "__main__":
    unittest.main()
