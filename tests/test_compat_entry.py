"""The legacy entry points must keep working through the toolkit."""
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

TOOLKIT_ROOT = Path(__file__).resolve().parents[1]
COMPAT = TOOLKIT_ROOT / "llm_api_benchmark.py"


def block_network():
    return mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))


class TestCompatEntryDryRun(unittest.TestCase):
    def test_dry_run_json_shape(self):
        proc = subprocess.run(
            [sys.executable, str(COMPAT), "--dry-run",
             "--base-url", "https://example.com/v1", "--model", "kimi-k3"],
            capture_output=True, text=True, cwd=str(TOOLKIT_ROOT), timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("deprecated", proc.stderr.lower())
        config = json.loads(proc.stdout)
        for key in ("script_version", "provider_name", "base_url", "model", "protocols",
                    "suites", "timeout", "concurrency", "input_sizes", "cache_requests",
                    "output_dir", "quick"):
            self.assertIn(key, config)
        self.assertEqual(config["model"], "kimi-k3")

    def test_missing_args_exit_2(self):
        proc = subprocess.run(
            [sys.executable, str(COMPAT), "--dry-run"],
            capture_output=True, text=True, cwd=str(TOOLKIT_ROOT), timeout=120,
        )
        self.assertEqual(proc.returncode, 2)

    def test_cache_requires_anthropic_exit_2(self):
        proc = subprocess.run(
            [sys.executable, str(COMPAT), "--dry-run",
             "--base-url", "https://example.com/v1", "--model", "m",
             "--protocols", "openai", "--suite", "cache"],
            capture_output=True, text=True, cwd=str(TOOLKIT_ROOT), timeout=120,
        )
        self.assertEqual(proc.returncode, 2)


class TestToolkitImportsFromCompat(unittest.TestCase):
    def test_compat_maps_to_toolkit_config(self):
        sys.path.insert(0, str(TOOLKIT_ROOT))
        sys.path.insert(0, str(TOOLKIT_ROOT / "src"))
        import llm_api_benchmark

        args = llm_api_benchmark.build_parser().parse_args([
            "--base-url", "https://example.com/v1", "--model", "kimi-k3",
            "--protocols", "openai,anthropic", "--suite", "cache",
        ])
        self.assertEqual(args.protocols, ["openai", "anthropic"])
        self.assertEqual(args.suite, ["cache"])


if __name__ == "__main__":
    unittest.main()
