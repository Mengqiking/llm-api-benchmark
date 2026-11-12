"""First-run contract: offline, self-contained, honest metrics and provenance."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from llm_bench.cli import main
from llm_bench.demo import run_demo
from llm_bench.legacy import LegacyRunError, reanalyze_run
from llm_bench.models import ProviderConfig

ROOT = Path(__file__).resolve().parents[1]


def read_summary(run):
    return json.loads(Path(run["summary"]).read_text(encoding="utf-8"))


class TestDemo(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target in ("socket.socket", "socket.getaddrinfo"):
            guard = mock.patch(target, side_effect=AssertionError(f"network attempted: {target}"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_demo_uses_real_reports_without_credentials_or_network(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = run_demo(self.root)
        self.assertFalse(result["network_requests_executed"])
        known = result["runs"]["includes_cache"]
        unknown = result["runs"]["unknown"]
        core = read_summary(known)["core"]
        self.assertEqual(core["ttft_p50_ms"], 120)
        self.assertEqual(core["ttft_p95_ms"], 240)
        self.assertEqual(core["ttft_missing_samples"], 2)
        self.assertEqual(core["formal_requests"], 8)
        self.assertEqual(core["error_rate"], 1 / 8)
        self.assertAlmostEqual(core["post_cold_request_hit_rate"], 2 / 3)
        self.assertAlmostEqual(core["post_cold_token_hit_rate"], 1700 / 3000)
        self.assertIsNone(read_summary(unknown)["core"]["post_cold_token_hit_rate"])
        for run in result["runs"].values():
            summary = read_summary(run)
            self.assertEqual(summary["manifest"]["data_kind"], "synthetic")
            self.assertFalse(summary["manifest"]["network_requests_executed"])
            report = Path(run["report"]).read_text(encoding="utf-8")
            self.assertIn("合成数据演示", report)
            self.assertIn("|全局错误率|12.50%|8|成功 7，失败 1", report)
            self.assertTrue(Path(run["requests_csv"]).is_file())
            records = list(Path(run["raw"]).glob("*.json"))
            self.assertEqual(len(records), 9)  # Includes one excluded preflight.
            for path in records:
                raw = path.read_text(encoding="utf-8")
                self.assertEqual(json.loads(raw)["request_headers"]["Authorization"],
                                 "[REDACTED]")

    def test_reanalysis_preserves_synthetic_provenance(self):
        demo = run_demo(self.root / "demo")
        known = demo["runs"]["includes_cache"]
        run_dir = Path(known["run_dir"])
        summary, output = reanalyze_run(run_dir, self.root / "reanalysis")
        self.assertEqual(summary["core"], read_summary(known)["core"])
        self.assertEqual(summary["manifest"]["data_kind"], "synthetic")
        self.assertEqual(summary["manifest"]["input_token_semantics_source"], "run_manifest")
        self.assertIn("合成数据演示", (output / "report" / "benchmark_report.md").read_text(encoding="utf-8"))

    def test_cli_reanalysis_reproduces_default_demo_without_overrides(self):
        demo = run_demo(self.root / "demo")
        for semantics, run in demo["runs"].items():
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["reanalyze", "--run", run["run_dir"],
                             "--output", str(self.root / "reanalyzed")])
            self.assertEqual(code, 0)
            output = json.loads(stdout.getvalue())
            summary = json.loads((Path(output["output"]) / "normalized" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["core"], read_summary(run)["core"])
            self.assertEqual(summary["cache"]["input_token_semantics"], semantics)

    def test_manifest_precedes_changed_provider_and_override_precedes_manifest(self):
        demo = run_demo(self.root / "demo")
        run = Path(demo["runs"]["includes_cache"]["run_dir"])
        provider = ProviderConfig(id="changed", display_name="Changed config",
                                  base_url="https://demo.invalid", default_model="demo-model",
                                  protocols=["openai"], api_key_env="NOT_USED",
                                  input_token_semantics={"openai": "excludes_cache"})
        preserved, _ = reanalyze_run(run, self.root / "preserved", provider=provider)
        self.assertAlmostEqual(preserved["core"]["post_cold_token_hit_rate"], 1700 / 3000)
        changed, _ = reanalyze_run(run, self.root / "changed", provider=provider,
                                  semantics_override={"openai": "excludes_cache"})
        self.assertAlmostEqual(changed["core"]["post_cold_token_hit_rate"], 1700 / 4700)
        self.assertEqual(changed["manifest"]["input_token_semantics_source"], "user_override")

    def test_malformed_recorded_semantics_rejected_before_writing_output(self):
        demo = run_demo(self.root / "demo")
        run = Path(demo["runs"]["includes_cache"]["run_dir"])
        path = run / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["input_token_semantics"] = {"openai": "guessed"}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        output = self.root / "invalid"
        with self.assertRaises(LegacyRunError):
            reanalyze_run(run, output)
        self.assertFalse(output.exists())

    def test_repeat_run_does_not_overwrite_previous_artifacts(self):
        first = run_demo(self.root)
        previous = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        second = run_demo(self.root)
        self.assertNotEqual(first["runs"]["includes_cache"]["report"],
                            second["runs"]["includes_cache"]["report"])
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)

    def test_cli_demo_has_machine_readable_output(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["demo", "--output", str(self.root)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["command"], "demo")

    def test_first_run_from_clean_copy_and_unrelated_working_directory(self):
        # No Git metadata, providers, profiles, tests or installed package.
        checkout = self.root / "clean checkout space 中文"
        shutil.copytree(ROOT / "src", checkout / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (checkout / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts" / "demo.py", checkout / "scripts" / "demo.py")
        outside = self.root / "outside space 中文"
        outside.mkdir()
        # -I excludes local installed user packages/PYTHONPATH. Guard the child
        # too: patching sockets in this process does not affect subprocesses.
        bootstrap = (
            "import runpy,sys; from unittest.mock import patch; "
            "sys.argv=[sys.argv[1]]; "
            "patch('socket.socket', side_effect=AssertionError('network attempted')).start(); "
            "patch('socket.getaddrinfo', side_effect=AssertionError('DNS lookup attempted')).start(); "
            "runpy.run_path(sys.argv[0], run_name='__main__')"
        )
        process = subprocess.run(
            [sys.executable, "-I", "-c", bootstrap, str(checkout / "scripts" / "demo.py")],
            cwd=outside, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        for run in result["runs"].values():
            self.assertTrue(Path(run["report"]).is_file())
            self.assertTrue(Path(run["run_dir"]).is_relative_to(outside / "runs" / "demo"))


if __name__ == "__main__":
    unittest.main()
