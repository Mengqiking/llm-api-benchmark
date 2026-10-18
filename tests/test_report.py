import json
import re
import tempfile
import unittest
from pathlib import Path

try:
    from tests.helpers import make_row
except ImportError:  # pragma: no cover
    from helpers import make_row  # type: ignore

from llm_bench.reporters import build_summary, render_markdown, write_requests_csv
from llm_bench.reporters.csv_report import CSV_COLUMNS

MANIFEST = {
    "tool_version": "0.1.0",
    "provider": "tostco",
    "provider_display_name": "Tostco",
    "base_url": "https://tostco.ai/v1",
    "model": "kimi-k3",
    "profile": "standard",
    "protocols": ["openai", "anthropic"],
    "started_at": "2026-10-15T10:00:00.000+08:00",
    "completed_at": "2026-10-15T10:05:00.000+08:00",
    "python": "3.12.10",
    "platform": "Windows-11",
    "params": {},
    "network_requests_executed": False,
    "secrets_persisted": False,
}


def sample_rows():
    rows = []
    for size, conc in ((1024, 1), (8192, 4)):
        for i in range(3):
            rows.append(make_row(
                request_id=f"lat_{size}_{conc}_{i}", test_name="latency", suite="latency",
                protocol="openai", sequence=i + 1, input_size_chars=size, concurrency=conc,
                stream=True, ttft_ms=100.0 + i * 50 + conc, ttft_valid=True, ttft_source="content",
                total_ms=800.0 + i * 100, first_event_ms=80.0, first_content_ms=100.0 + i * 50,
                input_tokens_raw=size // 2, cache_read_input_tokens=0,
                total_logical_input_tokens=size // 2, cache_hit=False,
                input_token_semantics="includes_cache", ttft_missing_reason=None,
            ))
    rows.append(make_row(sequence=1, ttft_ms=900.0, total_ms=1700.0, cache_hit_scope="any"))
    for seq in (2, 3):
        rows.append(make_row(
            request_id=f"cache_{seq}", sequence=seq,
            input_tokens_raw=1705, cache_read_input_tokens=1705,
            total_logical_input_tokens=3410, ttft_ms=300.0, total_ms=900.0,
            cache_hit_scope="target_prefix",
        ))
    rows.append(make_row(request_id="fail_1", test_name="smoke_chat", suite="smoke",
                         success=False, http_status=429, error="HTTPError: 429 rate limited"))
    return rows


class TestMarkdownReport(unittest.TestCase):
    def setUp(self):
        self.rows = sample_rows()
        self.summary = build_summary(self.rows, MANIFEST)
        self.markdown = render_markdown(self.summary)

    def test_core_summary_is_first_section(self):
        sections = [line for line in self.markdown.splitlines() if line.startswith("## ")]
        self.assertEqual(sections[0], "## 核心性能摘要")
        self.assertIn("|TTFT P50|", self.markdown)
        self.assertIn("|TTFT P95|", self.markdown)
        self.assertIn("|预热后请求缓存命中率|", self.markdown)
        self.assertIn("|预热后 Token 缓存命中率|", self.markdown)
        self.assertIn("|全局错误率|", self.markdown)
        self.assertIn("|TTFT 错误率|", self.markdown)

    def test_required_sections_present(self):
        for heading in ("## TTFT", "## 缓存命中", "## 测试组状态", "## 异常汇总", "## 数据口径与限制"):
            self.assertIn(heading, self.markdown)

    def test_cache_percentages_within_bounds_or_na(self):
        # every percentage printed in the report must be <= 100% or N/A
        for match in re.finditer(r"(\d+(?:\.\d+)?)%", self.markdown):
            self.assertLessEqual(float(match.group(1)), 100.0,
                                 f"发现超过 100% 的百分比: {match.group(0)}")

    def test_ttft_group_table_present(self):
        self.assertIn("### 按输入长度与并发分组", self.markdown)
        self.assertIn("|1024|1|", self.markdown)
        self.assertIn("|8192|4|", self.markdown)

    def test_failures_listed(self):
        self.assertIn("HTTP `429`", self.markdown)

    def test_inconsistent_cache_shows_na_and_warning(self):
        rows = [make_row(sequence=1, input_tokens_raw=100, cache_read_input_tokens=150,
                         total_logical_input_tokens=100, cache_metrics_valid=False,
                         cache_metrics_warning="缓存读取(150) 大于总逻辑输入(100)，命中率超过 100%",
                         input_token_semantics="includes_cache")]
        summary = build_summary(rows, MANIFEST)
        markdown = render_markdown(summary)
        self.assertIn("N/A", markdown)
        self.assertIn("命中率超过 100%", markdown)
        for match in re.finditer(r"(\d+(?:\.\d+)?)%", markdown):
            self.assertLessEqual(float(match.group(1)), 100.0)

    def test_reasoning_switch_capability_is_reported(self):
        rows = [
            make_row(request_id="off", test_name="reasoning_switch_off", suite="reasoning",
                     protocol="openai", reasoning_effort="none", success=True,
                     reasoning_chars=0, thinking_tokens=0),
            make_row(request_id="on", test_name="reasoning_effort", suite="reasoning",
                     protocol="openai", reasoning_effort="low", success=True,
                     reasoning_chars=12, thinking_tokens=8),
        ]
        summary = build_summary(rows, MANIFEST)
        self.assertTrue(summary["reasoning_switch"]["supported"])
        self.assertIn("支持，且输出侧观察到开关差异", render_markdown(summary))

    def test_reasoning_switch_rejection_is_reported(self):
        rows = [
            make_row(request_id="off", test_name="reasoning_switch_off", suite="reasoning",
                     protocol="openai", reasoning_effort="none", success=False,
                     http_status=400, error="unsupported effort"),
            make_row(request_id="on", test_name="reasoning_effort", suite="reasoning",
                     protocol="openai", reasoning_effort="low", success=True),
        ]
        summary = build_summary(rows, MANIFEST)
        self.assertFalse(summary["reasoning_switch"]["supported"])
        self.assertIn("不支持通过 reasoning_effort=none 关闭", render_markdown(summary))


class TestJsonSummary(unittest.TestCase):
    def test_json_matches_markdown_core_metrics(self):
        rows = sample_rows()
        summary = build_summary(rows, MANIFEST)
        markdown = render_markdown(summary)
        core = summary["core"]
        expected_p50 = core["ttft_p50_ms"]
        self.assertIn(f"{expected_p50:.1f}", markdown)
        post_rate = core["post_cold_request_hit_rate"]
        self.assertIn(f"{post_rate:.2%}", markdown)
        self.assertEqual(core["requests"], len(rows))
        self.assertEqual(summary["manifest"], MANIFEST)
        self.assertIn("suite_status", summary)
        self.assertEqual(summary["suite_status"]["cache"], "3/3 成功")

    def test_schema_version_present(self):
        summary = build_summary(sample_rows(), MANIFEST)
        self.assertTrue(summary["schema_version"] >= 5)


class TestCsvReport(unittest.TestCase):
    def test_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requests.csv"
            rows = sample_rows()
            write_requests_csv(path, rows)
            text = path.read_text(encoding="utf-8-sig")
            header = text.splitlines()[0].split(",")
            self.assertEqual(header, CSV_COLUMNS)
            self.assertEqual(len(text.splitlines()), len(rows) + 1)

    def test_csv_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requests.csv"
            write_requests_csv(path, [])
            self.assertEqual(path.read_text(encoding="utf-8-sig"), "")


if __name__ == "__main__":
    unittest.main()
