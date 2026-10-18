import unittest

try:
    from tests.helpers import make_row
except ImportError:  # pragma: no cover
    from helpers import make_row  # type: ignore

from llm_bench.metrics.latency import percentile_nearest_rank, summarize_ttft


class TestPercentileNearestRank(unittest.TestCase):
    def test_odd_samples(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(percentile_nearest_rank(values, 50), 30.0)
        self.assertEqual(percentile_nearest_rank(values, 90), 50.0)
        self.assertEqual(percentile_nearest_rank(values, 95), 50.0)
        self.assertEqual(percentile_nearest_rank(values, 99), 50.0)

    def test_even_samples(self):
        values = [1.0, 2.0, 3.0, 4.0]
        # rank = ceil(0.5*4) = 2 -> 2.0
        self.assertEqual(percentile_nearest_rank(values, 50), 2.0)
        # rank = ceil(0.95*4) = 4 -> 4.0
        self.assertEqual(percentile_nearest_rank(values, 95), 4.0)

    def test_unordered_input(self):
        self.assertEqual(percentile_nearest_rank([30.0, 10.0, 20.0], 50), 20.0)

    def test_single_and_empty(self):
        self.assertEqual(percentile_nearest_rank([7.5], 99), 7.5)
        self.assertIsNone(percentile_nearest_rank([], 50))

    def test_bounds(self):
        values = [5.0, 1.0, 3.0]
        self.assertEqual(percentile_nearest_rank(values, 0), 1.0)
        self.assertEqual(percentile_nearest_rank(values, 100), 5.0)


class TestSummarizeTtft(unittest.TestCase):
    def test_overall_counts_and_percentiles(self):
        rows = [make_row(ttft_ms=float(100 * i), ttft_valid=True, suite="latency") for i in range(1, 6)]
        summary = summarize_ttft(rows)
        self.assertEqual(summary["attempted"], 5)
        self.assertEqual(summary["successful"], 5)
        self.assertEqual(summary["valid_samples"], 5)
        self.assertEqual(summary["missing_samples"], 0)
        self.assertEqual(summary["ttft_p50_ms"], 300.0)
        self.assertEqual(summary["ttft_p95_ms"], 500.0)
        self.assertEqual(summary["error_rate"], 0.0)

    def test_missing_samples_counted(self):
        rows = [
            make_row(ttft_ms=100.0, ttft_valid=True, suite="latency"),
            make_row(ttft_ms=None, ttft_valid=False, ttft_missing_reason="no_semantic_delta", suite="latency"),
            make_row(success=False, ttft_ms=None, ttft_valid=False,
                     ttft_missing_reason="http_error", suite="latency"),
        ]
        summary = summarize_ttft(rows)
        self.assertEqual(summary["attempted"], 3)
        self.assertEqual(summary["successful"], 2)
        self.assertEqual(summary["valid_samples"], 1)
        # streamed failures count as missing TTFT as well
        self.assertEqual(summary["missing_samples"], 2)
        self.assertEqual(summary["missing_reasons"].get("no_semantic_delta"), 1)
        self.assertEqual(summary["missing_reasons"].get("http_error"), 1)
        self.assertAlmostEqual(summary["error_rate"], 1 / 3)

    def test_grouping_by_input_and_concurrency(self):
        rows = []
        for size in (1024, 8192):
            for conc in (1, 4):
                for _ in range(3):
                    rows.append(make_row(
                        suite="latency", ttft_valid=True,
                        ttft_ms=float(size + conc),
                        input_size_chars=size, concurrency=conc,
                    ))
        summary = summarize_ttft(rows, group_keys=["input_size_chars", "concurrency"])
        groups = summary["groups"]
        self.assertEqual(len(groups), 4)
        by_key = {tuple(sorted(g["group"].items())): g for g in groups}
        key = tuple(sorted({"input_size_chars": 8192, "concurrency": 4}.items()))
        self.assertEqual(by_key[key]["ttft_p50_ms"], 8196.0)

    def test_empty_rows(self):
        summary = summarize_ttft([])
        self.assertEqual(summary["attempted"], 0)
        self.assertIsNone(summary["ttft_p50_ms"])
        self.assertIsNone(summary["error_rate"])


if __name__ == "__main__":
    unittest.main()
