"""CSV output for normalized rows."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

CSV_COLUMNS = [
    "request_id",
    "test_name",
    "suite",
    "protocol",
    "provider",
    "model",
    "started_at",
    "completed_at",
    "success",
    "http_status",
    "error",
    "input_size_chars",
    "concurrency",
    "reasoning_effort",
    "sequence",
    "phase",
    "stream",
    "request_started_at",
    "first_byte_ms",
    "first_event_ms",
    "first_reasoning_ms",
    "first_content_ms",
    "ttft_ms",
    "total_ms",
    "ttft_source",
    "ttft_valid",
    "ttft_missing_reason",
    "input_tokens_raw",
    "uncached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "total_logical_input_tokens",
    "input_token_semantics",
    "cache_metric_source",
    "cache_hit",
    "cache_hit_scope",
    "cache_metrics_valid",
    "cache_metrics_warning",
    "cold_baseline_cache_read_tokens",
    "target_prefix_cache_read_tokens",
    "target_prefix_evidence",
    "output_tokens",
    "thinking_tokens",
    "reasoning_chars",
    "content_chars",
]


def write_requests_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in CSV_COLUMNS})


def write_requests_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def load_normalized_rows(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))
