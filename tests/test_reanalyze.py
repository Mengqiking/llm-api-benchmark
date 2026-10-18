import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_bench.legacy import LegacyRunError, reanalyze_run
from llm_bench.reporters.csv_report import load_normalized_rows

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "historical_tostco"
TOOLKIT = Path(__file__).resolve().parents[1]


def block_network():
    """Patch socket construction so any network attempt fails the test."""
    return mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))


class TestReanalyzeHistoricalFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.run_dir = self.work / "copy_of_history"
        shutil.copytree(FIXTURE, self.run_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reanalyze_without_network(self):
        with block_network():
            summary, out_dir = reanalyze_run(self.run_dir, self.work / "out")
        rows = load_normalized_rows(out_dir / "normalized" / "requests.json")
        self.assertEqual(len(rows), 3)
        self.assertEqual(out_dir.name.startswith("copy_of_history"), True)
        # no network flag must be explicit
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["network_requests_executed"])
        self.assertFalse(manifest["secrets_persisted"])
        self.assertEqual(manifest["legacy_source_kind"], "raw")

    def test_cache_metrics_from_anthropic_history(self):
        # default: no provider config, no override -> semantics unknown,
        # no pseudo-precise token hit rate; raw fields are preserved
        with block_network():
            summary, out_dir = reanalyze_run(self.run_dir, self.work / "out")
        rows = load_normalized_rows(out_dir / "normalized" / "requests.json")
        for row in rows:
            self.assertEqual(row["suite"], "cache")
            self.assertEqual(row["protocol"], "anthropic")
            self.assertEqual(row["input_token_semantics"], "unknown")
            self.assertIsNone(row["total_logical_input_tokens"])
            self.assertFalse(row["cache_metrics_valid"])
        cache = summary["cache"]
        self.assertEqual(cache["attempted"], 3)
        self.assertEqual(cache["successful"], 3)
        self.assertEqual(cache["cold_start"]["sequence"], 1)
        self.assertEqual(cache["post_cold"]["attempted"], 2)
        self.assertIsNone(cache["token_hit_rate"])
        self.assertIsNone(cache["post_cold"]["token_hit_rate"])
        self.assertEqual(cache["input_token_semantics"], "unknown")
        # target-prefix evidence does not need token semantics:
        # cold baseline 64, warm reads 1664 > 64
        self.assertEqual(cache["cold_baseline_cache_read_tokens"], 64)
        scopes = {r["sequence"]: r["cache_hit_scope"] for r in cache["requests"]}
        self.assertEqual(scopes[1], "any")
        self.assertEqual(scopes[2], "target_prefix")
        self.assertEqual(scopes[3], "target_prefix")
        self.assertEqual(cache["post_cold"]["request_hits"], 2)
        self.assertEqual(cache["post_cold"]["target_prefix_increment_tokens"], (1664 - 64) * 2)

    def test_explicit_includes_cache_override(self):
        # fixture warm rows: input 1705, cache_read 1664 (includes_cache)
        # -> post-cold token rate = 3328/3410 ~= 97.60%
        with block_network():
            summary, out_dir = reanalyze_run(
                self.run_dir, self.work / "out_inc",
                semantics_override={"anthropic": "includes_cache"},
            )
        rows = load_normalized_rows(out_dir / "normalized" / "requests.json")
        for row in rows:
            self.assertEqual(row["input_token_semantics"], "includes_cache")
            self.assertEqual(row["total_logical_input_tokens"], row["input_tokens_raw"])
        rate = summary["cache"]["post_cold"]["token_hit_rate"]
        self.assertIsNotNone(rate)
        self.assertAlmostEqual(rate, 3328 / 3410, places=4)
        self.assertLessEqual(rate, 1.0)
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["input_token_semantics_source"], "user_override")

    def test_explicit_excludes_cache_override(self):
        # excludes_cache -> post-cold token rate = 3328/(3410+3328) ~= 49.39%
        with block_network():
            summary, out_dir = reanalyze_run(
                self.run_dir, self.work / "out_exc",
                semantics_override={"anthropic": "excludes_cache"},
            )
        rate = summary["cache"]["post_cold"]["token_hit_rate"]
        self.assertIsNotNone(rate)
        self.assertAlmostEqual(rate, 3328 / 6738, places=4)
        self.assertLessEqual(rate, 1.0)
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["input_token_semantics_source"], "user_override")

    def test_semantics_warning_present(self):
        with block_network():
            summary, _ = reanalyze_run(self.run_dir, self.work / "out")
        self.assertTrue(any("语义" in w and "unknown" in w for w in summary["warnings"]))

    def test_report_written(self):
        with block_network():
            _summary, out_dir = reanalyze_run(self.run_dir, self.work / "out")
        report = (out_dir / "report" / "benchmark_report.md").read_text(encoding="utf-8")
        self.assertIn("# Benchmark 报告", report)
        self.assertIn("## 核心性能摘要", report)
        self.assertIn("## 缓存命中", report)
        # unknown semantics must be labelled, never a pseudo-precise rate
        self.assertIn("N/A（unknown）", report)
        self.assertIn("语义未知，不输出伪精确命中率", report)

    def test_original_directory_untouched(self):
        before = {
            p.relative_to(self.run_dir): p.read_bytes()
            for p in sorted(self.run_dir.rglob("*")) if p.is_file()
        }
        with block_network():
            reanalyze_run(self.run_dir, self.work / "out")
        after = {
            p.relative_to(self.run_dir): p.read_bytes()
            for p in sorted(self.run_dir.rglob("*")) if p.is_file()
        }
        self.assertEqual(before, after)

    def test_reanalyze_directly_from_original_fixture(self):
        # reading straight out of the (read-only) real fixture directory
        with block_network():
            summary, out_dir = reanalyze_run(FIXTURE, self.work / "out2")
        self.assertEqual(summary["core"]["requests"], 3)
        self.assertTrue((out_dir / "report" / "benchmark_report.md").is_file())


class TestCleanedOnlyFallback(unittest.TestCase):
    def test_cleaned_only_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "legacy_run"
            (run_dir / "cleaned").mkdir(parents=True)
            rows = [
                {
                    "request_id": "cache_c1", "test_name": "cache", "protocol": "anthropic",
                    "success": True, "http_status": 200, "total_ms": 1700.78, "ttft_ms": None,
                    "first_content_ms": None, "reasoning_chars": 0, "content_chars": 0,
                    "input_tokens": 1705, "output_tokens": 8, "thinking_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 64,
                    "input_size_chars": None, "concurrency": None, "reasoning_effort": None,
                    "sequence": 1, "error": "",
                },
                {
                    "request_id": "cache_c2", "test_name": "cache", "protocol": "anthropic",
                    "success": True, "http_status": 200, "total_ms": 900.5, "ttft_ms": None,
                    "first_content_ms": None, "reasoning_chars": 0, "content_chars": 0,
                    "input_tokens": 1705, "output_tokens": 8, "thinking_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1705,
                    "input_size_chars": None, "concurrency": None, "reasoning_effort": None,
                    "sequence": 2, "error": "",
                },
            ]
            (run_dir / "cleaned" / "requests.json").write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8"
            )
            with block_network():
                summary, out_dir = reanalyze_run(run_dir, Path(tmp) / "out")
            adapted = load_normalized_rows(out_dir / "normalized" / "requests.json")
            self.assertEqual(len(adapted), 2)
            # default is unknown semantics: raw fields kept, no pseudo rate
            self.assertEqual(adapted[0]["input_token_semantics"], "unknown")
            self.assertIsNone(adapted[0]["total_logical_input_tokens"])
            cache = summary["cache"]
            self.assertEqual(cache["attempted"], 2)
            self.assertIsNone(cache["token_hit_rate"])
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["legacy_source_kind"], "cleaned")


class TestProviderMatching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.run_dir = self.work / "copy_of_history"
        shutil.copytree(FIXTURE, self.run_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unique_evidence_matches_tostco(self):
        from llm_bench.legacy import match_provider_from_manifest

        manifest = json.loads((self.run_dir / "raw" / "manifest.json").read_text(encoding="utf-8"))
        provider, evidence = match_provider_from_manifest(manifest, TOOLKIT / "providers")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.id, "tostco")
        self.assertIn("base_url", evidence)

    def test_ambiguous_evidence_returns_none(self):
        from llm_bench.legacy import match_provider_from_manifest

        manifest = {"provider_name": "Tostco", "base_url": "https://tostco.ai/v1"}
        # duplicate tostco under a different id -> two candidates, no unique match
        dup_dir = self.work / "providers_dup"
        dup_dir.mkdir()
        data = json.loads((TOOLKIT / "providers" / "tostco.json").read_text(encoding="utf-8"))
        data["id"] = "tostco_mirror"
        (dup_dir / "tostco_mirror.json").write_text(json.dumps(data), encoding="utf-8")
        original = json.loads((TOOLKIT / "providers" / "tostco.json").read_text(encoding="utf-8"))
        (dup_dir / "tostco.json").write_text(json.dumps(original), encoding="utf-8")
        provider, evidence = match_provider_from_manifest(manifest, dup_dir)
        self.assertIsNone(provider)
        self.assertEqual(evidence, "")

    def test_no_evidence_returns_none(self):
        from llm_bench.legacy import match_provider_from_manifest

        provider, evidence = match_provider_from_manifest({}, TOOLKIT / "providers")
        self.assertIsNone(provider)
        self.assertEqual(evidence, "")

    def test_explicit_provider_config_semantics(self):
        # matched provider config supplies its declared semantics
        from llm_bench.config import load_provider
        from llm_bench.legacy import reanalyze_run as _reanalyze  # noqa: F401

        provider = load_provider("tokenstars", TOOLKIT / "providers")
        with block_network():
            summary, out_dir = reanalyze_run(
                self.run_dir, self.work / "out_tok", provider=provider,
                provider_evidence="test-explicit",
            )
        # tokenstars declares anthropic=excludes_cache -> rates computed
        self.assertEqual(summary["cache"]["input_token_semantics"], "excludes_cache")
        self.assertIsNotNone(summary["cache"]["token_hit_rate"])
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["input_token_semantics_source"], "provider_config")


class TestCliReanalyzeOverrides(unittest.TestCase):
    def _run_cli(self, argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from llm_bench.cli import main as cli_main

        buffer_out, buffer_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buffer_out), redirect_stderr(buffer_err):
            code = cli_main(argv)
        return code, buffer_out.getvalue(), buffer_err.getvalue()

    def test_invalid_override_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = self._run_cli([
                "reanalyze", "--run", str(FIXTURE),
                "--output", str(Path(tmp) / "out"),
                "--input-token-semantics", "anthropic=magic",
            ])
        self.assertEqual(code, 2)

    def test_malformed_override_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = self._run_cli([
                "reanalyze", "--run", str(FIXTURE),
                "--output", str(Path(tmp) / "out"),
                "--input-token-semantics", "anthropic-includes_cache",
            ])
        self.assertEqual(code, 2)

    def test_unknown_provider_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = self._run_cli([
                "reanalyze", "--run", str(FIXTURE),
                "--output", str(Path(tmp) / "out"),
                "--provider", "ghost",
            ])
        self.assertEqual(code, 2)

    def test_cli_explicit_semantics_and_output(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self._run_cli([
                "reanalyze", "--run", str(FIXTURE),
                "--output", str(Path(tmp) / "out"),
                "--input-token-semantics", "anthropic=includes_cache",
            ])
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertFalse(payload["network_requests_executed"])
            self.assertEqual(payload["input_token_semantics_source"], "user_override")
            summary = json.loads(
                (Path(payload["output"]) / "normalized" / "summary.json").read_text(encoding="utf-8")
            )
            rate = summary["cache"]["post_cold"]["token_hit_rate"]
            self.assertAlmostEqual(rate, 3328 / 3410, places=4)

    def test_cli_auto_detected_provider_config(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self._run_cli([
                "reanalyze", "--run", str(FIXTURE),
                "--output", str(Path(tmp) / "out"),
            ])
            self.assertEqual(code, 0)
            payload = json.loads(out)
            # fixture manifest uniquely matches tostco -> provider config applied
            self.assertEqual(payload["provider"], "tostco")
            self.assertTrue(payload["provider_evidence"])
            self.assertEqual(payload["input_token_semantics_source"], "provider_config")


class TestReanalyzeErrors(unittest.TestCase):
    def test_missing_dir(self):
        with self.assertRaises(LegacyRunError):
            reanalyze_run(Path("Z:/definitely/not/here"), Path(tempfile.gettempdir()) / "llmbench_out")

    def test_unrecognized_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "not_a_run"
            empty.mkdir()
            with self.assertRaises(LegacyRunError):
                reanalyze_run(empty, Path(tmp) / "out")


if __name__ == "__main__":
    unittest.main()
