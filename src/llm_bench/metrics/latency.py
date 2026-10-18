"""TTFT and latency statistics.

Percentile algorithm: nearest-rank (matching the historical scripts).
For n samples and percentile p, rank = ceil(p/100 * n) clamped to >= 1,
result = sorted_values[rank - 1]. An empty sample list yields None.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable

GROUP_KEYS = ("provider", "model", "protocol", "input_size_chars", "concurrency", "reasoning_effort")

# Only streamed benchmark requests can produce a meaningful TTFT.
# smoke/video are non-streaming; preflight is infrastructure, not a benchmark.
TTFT_ELIGIBLE_SUITES = ("reasoning", "latency", "cache")


def is_ttft_eligible(row: dict[str, Any]) -> bool:
    """A request can be counted towards TTFT only if it was streamed and
    belongs to a TTFT-relevant benchmark suite. Non-streaming requests are NOT
    'missing TTFT' samples - they are simply out of scope."""
    return bool(row.get("stream")) and str(row.get("suite")) in TTFT_ELIGIBLE_SUITES


def percentile_nearest_rank(values: Iterable[float], p: int) -> float | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _rate(failures: int, attempted: int) -> float | None:
    return failures / attempted if attempted else None


def summarize_ttft(rows: list[dict[str, Any]], group_keys: list[str] | None = None) -> dict[str, Any]:
    """Summary over rows that are normalized request dicts.

    Callers are expected to pre-filter with is_ttft_eligible(). Rows missing
    any group key (e.g. cache probes without input_size_chars) are excluded
    from groups instead of forming a None/None bucket.
    """
    group_keys = [k for k in (group_keys or []) if k in GROUP_KEYS]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        if any(row.get(k) is None for k in group_keys):
            continue
        key = tuple(row.get(k) for k in group_keys)
        groups.setdefault(key, []).append(row)

    def summarize_subset(subset: list[dict[str, Any]]) -> dict[str, Any]:
        attempted = len(subset)
        successful = [r for r in subset if r.get("success")]
        valid = [float(r["ttft_ms"]) for r in successful if r.get("ttft_valid") and r.get("ttft_ms") is not None]
        # Missing TTFT = eligible requests that produced no valid TTFT:
        # streamed 2xx without a semantic delta, streamed HTTP errors and
        # streamed transport errors. Failures are part of missing, not silent.
        missing = [r for r in subset if not (r.get("ttft_valid") and r.get("ttft_ms") is not None)]
        reasons = Counter(
            str(r.get("ttft_missing_reason") or ("http_error" if not r.get("success") else "no_semantic_delta"))
            for r in missing
        )
        failures = attempted - len(successful)
        totals = [float(r["total_ms"]) for r in successful if r.get("total_ms") is not None]
        result: dict[str, Any] = {
            "attempted": attempted,
            "successful": len(successful),
            "valid_samples": len(valid),
            "missing_samples": len(missing),
            "missing_reasons": dict(reasons),
            "ttft_p50_ms": percentile_nearest_rank(valid, 50),
            "ttft_p90_ms": percentile_nearest_rank(valid, 90),
            "ttft_p95_ms": percentile_nearest_rank(valid, 95),
            "ttft_p99_ms": percentile_nearest_rank(valid, 99),
            "ttft_min_ms": min(valid) if valid else None,
            "ttft_max_ms": max(valid) if valid else None,
            "total_p50_ms": percentile_nearest_rank(totals, 50),
            "total_p95_ms": percentile_nearest_rank(totals, 95),
            "error_rate": _rate(failures, attempted),
        }
        return result

    overall = summarize_subset(rows)
    overall["groups"] = []
    for key, subset in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        entry = summarize_subset(subset)
        entry["group"] = {k: v for k, v in zip(group_keys, key)}
        overall["groups"].append(entry)
    return overall
