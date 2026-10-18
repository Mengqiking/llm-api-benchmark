"""Cache-hit metrics with protocol-aware input-token semantics.

Semantics:
- excludes_cache (Anthropic): usage.input_tokens excludes cache tokens, so
  total_logical_input_tokens = input_tokens + cache_creation + cache_read.
- includes_cache (OpenAI): usage.prompt_tokens already includes cached_tokens,
  so total_logical_input_tokens = input_tokens.
- unknown: no pseudo-precise hit rate is produced; raw fields are kept and a
  warning is emitted.

Hit rates are never allowed to exceed 100% silently: an inconsistent input
(sample where cache_read > total_logical) invalidates the value and records a
warning instead of being reported.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any


def safe_rate(numerator: float | None, denominator: float | None) -> tuple[float | None, str | None]:
    """Return (rate, warning). Rates > 1 are rejected as data inconsistency."""
    if numerator is None or denominator is None:
        return None, None
    if denominator <= 0:
        return None, None
    rate = numerator / denominator
    if rate > 1.0:
        return None, (
            f"缓存读取({numerator}) 大于总逻辑输入({denominator})，"
            "命中率超过 100%，判定为数据不一致，已拒绝输出该值"
        )
    return rate, None


def determine_hit_scope(
    *,
    cache_read: int,
    is_cache_test: bool,
    sequence: int | None,
    first_success_sequence: int | None,
    cold_baseline_cache_read: int | None = None,
    phase: str | None = None,
) -> str:
    """Classify the observed cache read.

    - none: no cache read at all
    - any: cache read that cannot be attributed to this run's target prefix
      (cold-start requests, or warm reads not exceeding the cold baseline -
      the read may come from a gateway/system-prefix cache)
    - target_prefix: a warm request whose cache read exceeds the cold-start
      baseline, i.e. the incremental cached tokens are attributable to this
      run's unique prefix
    - unknown: cannot tell (non-cache tests, missing sequence/baseline info)
    """
    if cache_read <= 0:
        return "none"
    if not is_cache_test:
        return "unknown"
    if sequence is None or first_success_sequence is None:
        return "unknown"
    in_cold = phase == "cold_start" or (phase is None and sequence <= first_success_sequence)
    if in_cold:
        return "any"
    # Warm request: only reads exceeding the cold-start baseline are evidence
    # of target-prefix caching.
    if cold_baseline_cache_read is None:
        return "unknown"
    return "target_prefix" if cache_read > cold_baseline_cache_read else "any"


def cold_baseline_from_rows(rows: list[dict[str, Any]], first_success_sequence: int | None) -> int | None:
    """Cold-start baseline = the highest cache_read observed on successful
    cold-phase requests. With several cold requests this is conservative:
    warm reads must beat the best cold observation to count as evidence."""
    if first_success_sequence is None:
        return None
    reads: list[int] = []
    for row in rows:
        if not row.get("success"):
            continue
        if row.get("cache_read_input_tokens") is None:
            continue
        seq = row.get("sequence")
        if seq is None:
            continue
        phase = row.get("phase")
        in_cold = phase == "cold_start" or (
            phase is None and int(seq) <= first_success_sequence
        )
        if in_cold:
            reads.append(int(row["cache_read_input_tokens"]))
    return max(reads) if reads else None


def normalize_cache_fields(
    raw: dict[str, Any],
    semantics: str,
    *,
    is_cache_test: bool,
    sequence: int | None,
    first_success_sequence: int | None,
    cache_expected: bool = False,
    cold_baseline_cache_read: int | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    """Build the normalized cache field block for one request.

    `raw` comes from protocol.usage_fields(); `semantics` is one of
    excludes_cache / includes_cache / unknown.
    """
    input_raw = int(raw.get("input_tokens_raw") or 0)
    creation = int(raw.get("cache_creation_input_tokens") or 0)
    read = int(raw.get("cache_read_input_tokens") or 0)
    warnings: list[str] = []

    if semantics == "excludes_cache":
        total = input_raw + creation + read
        uncached = input_raw
    elif semantics == "includes_cache":
        total = input_raw
        uncached = input_raw - read - creation
        if uncached < 0:
            warnings.append(
                f"input_tokens({input_raw}) 小于 cache_read+cache_creation({read + creation})，"
                "input_token_semantics 声明为 includes_cache，数据可能不一致"
            )
            uncached = 0
    else:
        total = None
        uncached = None
        if read or creation:
            warnings.append("input_token_semantics 未知，无法给出可信的缓存命中率，仅保留原始字段")

    rate, rate_warning = safe_rate(read, total)
    if rate_warning:
        warnings.append(rate_warning)

    valid = semantics != "unknown" and rate_warning is None
    if cache_expected and not (read or creation):
        valid = False
        warnings.append("usage 中未发现任何缓存字段")

    source = raw.get("cache_field_path") or ("none" if not (read or creation) else "unknown")

    scope = determine_hit_scope(
        cache_read=read,
        is_cache_test=is_cache_test,
        sequence=sequence,
        first_success_sequence=first_success_sequence,
        cold_baseline_cache_read=cold_baseline_cache_read,
        phase=phase,
    )
    if is_cache_test and cold_baseline_cache_read is not None:
        target_prefix_read = max(read - cold_baseline_cache_read, 0)
        target_prefix_evidence: bool | None = read > cold_baseline_cache_read
    else:
        target_prefix_read = None
        target_prefix_evidence = None

    return {
        "input_tokens_raw": input_raw,
        "uncached_input_tokens": uncached,
        "cache_creation_input_tokens": creation,
        "cache_read_input_tokens": read,
        "total_logical_input_tokens": total,
        "input_token_semantics": semantics,
        "cache_metric_source": source,
        "cache_hit": read > 0,
        "cache_hit_scope": scope,
        "cold_baseline_cache_read_tokens": cold_baseline_cache_read if is_cache_test else None,
        "target_prefix_cache_read_tokens": target_prefix_read,
        "target_prefix_evidence": target_prefix_evidence,
        "cache_metrics_valid": valid,
        "cache_metrics_warning": "；".join(warnings) if warnings else None,
    }


def _mode(values: list[Any]) -> Any:
    counter = Counter(v for v in values if v is not None)
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _pct(values: list[float], p: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize_cache(rows: list[dict[str, Any]], cache_test_name: str = "cache") -> dict[str, Any]:
    """Aggregate cache metrics over normalized rows (cache-suite requests only)."""
    test_names = {r.get("test_name") for r in rows}
    if cache_test_name in test_names:
        items = [r for r in rows if r.get("test_name") == cache_test_name]
    else:
        items = [r for r in rows if r.get("suite") == "cache"]
    items = sorted(items, key=lambda r: (r.get("sequence") is None, r.get("sequence") or 0))
    successful = [r for r in items if r.get("success")]
    warnings: list[str] = []

    first_success_sequence = None
    for row in successful:
        if row.get("sequence") is not None:
            first_success_sequence = int(row["sequence"])  # type: ignore[arg-type]
            break

    cold_baseline = cold_baseline_from_rows(successful, first_success_sequence)

    def _is_warm(row: dict[str, Any]) -> bool:
        seq = row.get("sequence")
        if row.get("phase") == "warm":
            return True
        if row.get("phase") == "cold_start":
            return False
        return seq is not None and first_success_sequence is not None and int(seq) > first_success_sequence  # type: ignore[arg-type]

    if cold_baseline is None:
        warm_with_read = [
            r
            for r in successful
            if int(r.get("cache_read_input_tokens") or 0) > 0 and _is_warm(r)
        ]
        if warm_with_read:
            warnings.append("缺少冷启动基线，无法判定目标前缀命中；相关请求的 scope 记为 unknown")

    countable = [r for r in successful if r.get("cache_metrics_valid")]
    for row in successful:
        if row.get("cache_metrics_warning"):
            seq = row.get("sequence")
            prefix = f"#{seq}" if seq is not None else f"#{row.get('request_id', '?')}"
            warnings.append(f"{prefix}: {row['cache_metrics_warning']}")

    def _sum(field: str, subset: list[dict[str, Any]]) -> int:
        return sum(int(r.get(field) or 0) for r in subset)

    read_total = _sum("cache_read_input_tokens", countable)
    total_logical = _sum("total_logical_input_tokens", countable)
    token_rate, token_warning = safe_rate(read_total, total_logical)
    if token_warning:
        warnings.append(f"Token 加权命中率：{token_warning}")

    post_cold = [r for r in successful if _is_warm(r)]
    post_cold_read = _sum("cache_read_input_tokens", post_cold)
    post_cold_total = _sum("total_logical_input_tokens", post_cold)
    post_cold_token_rate, post_cold_token_warning = safe_rate(post_cold_read, post_cold_total)
    if post_cold_token_warning:
        warnings.append(f"预热后 Token 加权命中率：{post_cold_token_warning}")

    def _target_prefix_hit(row: dict[str, Any]) -> bool:
        if cold_baseline is None:
            return False
        return _is_warm(row) and int(row.get("cache_read_input_tokens") or 0) > cold_baseline

    hits = [r for r in successful if r.get("cache_hit")]
    misses = [r for r in successful if not r.get("cache_hit")]
    target_prefix_hits = [r for r in successful if _target_prefix_hit(r)]
    post_cold_target_hits = sum(1 for r in post_cold if _target_prefix_hit(r))
    # Incremental tokens attributable to the run's own prefix (gateway/system
    # prefix caching, already visible in the cold baseline, is subtracted).
    target_prefix_read_total = sum(
        max(int(r.get("cache_read_input_tokens") or 0) - cold_baseline, 0)
        for r in successful
        if _target_prefix_hit(r)
    )
    post_cold_target_read_total = sum(
        max(int(r.get("cache_read_input_tokens") or 0) - cold_baseline, 0)
        for r in post_cold
        if _target_prefix_hit(r)
    )

    def _ttft(subset: list[dict[str, Any]]) -> dict[str, float | None]:
        values = [float(r["ttft_ms"]) for r in subset if r.get("ttft_valid") and r.get("ttft_ms") is not None]
        totals = [float(r["total_ms"]) for r in subset if r.get("total_ms") is not None]
        return {
            "ttft_p50_ms": _pct(values, 50),
            "ttft_p95_ms": _pct(values, 95),
            "total_p50_ms": _pct(totals, 50),
            "total_p95_ms": _pct(totals, 95),
        }

    request_rate, request_warning = safe_rate(len(hits), len(successful))
    if request_warning:
        warnings.append(f"请求命中率：{request_warning}")
    any_post_cold_hits = sum(1 for r in post_cold if r.get("cache_hit"))
    post_cold_any_request_rate = (
        any_post_cold_hits / len(post_cold) if post_cold else None
    )
    post_cold_target_request_rate = (
        post_cold_target_hits / len(post_cold) if post_cold else None
    )

    if items and not countable:
        warnings.append("没有可统计的缓存样本（字段缺失或语义未知）")

    semantics_mode = _mode([r.get("input_token_semantics") for r in successful])
    if semantics_mode is None:
        semantics_mode = "unknown"

    return {
        "attempted": len(items),
        "successful": len(successful),
        "countable": len(countable),
        "request_hits": len(hits),
        "request_hit_rate": request_rate,
        "target_prefix_request_hits": len(target_prefix_hits),
        "target_prefix_request_hit_rate": (
            len(target_prefix_hits) / len(successful) if successful else None
        ),
        "cold_baseline_cache_read_tokens": cold_baseline,
        "cold_start": {
            "sequence": first_success_sequence,
            "successful": len(successful) - len(post_cold),
            "scope_note": "首个成功请求的 cache_read 可能来自网关/系统前缀缓存，记为 any",
        },
        "post_cold": {
            "attempted": len(post_cold),
            # target-prefix hits only: warm reads exceeding the cold baseline
            "request_hits": post_cold_target_hits,
            "request_hit_rate": post_cold_target_request_rate,
            "any_request_hits": any_post_cold_hits,
            "any_request_hit_rate": post_cold_any_request_rate,
            "token_hit_rate": post_cold_token_rate,
            "target_prefix_increment_tokens": post_cold_target_read_total,
        },
        "target_prefix_cache_read_tokens": target_prefix_read_total,
        "token_hit_rate": token_rate,
        "cache_creation_input_tokens": _sum("cache_creation_input_tokens", countable),
        "cache_read_input_tokens": read_total,
        "uncached_input_tokens": _sum("uncached_input_tokens", countable),
        "total_logical_input_tokens": total_logical,
        "hit_ttft": _ttft(hits),
        "miss_ttft": _ttft(misses),
        "cache_metric_source": _mode([r.get("cache_metric_source") for r in countable]),
        "input_token_semantics": semantics_mode,
        "warnings": warnings,
        "requests": [
            {
                "sequence": r.get("sequence"),
                "request_id": r.get("request_id"),
                "phase": r.get("phase") or ("warm" if _is_warm(r) else None),
                "success": bool(r.get("success")),
                "input_tokens_raw": r.get("input_tokens_raw"),
                "cache_creation_input_tokens": r.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": r.get("cache_read_input_tokens"),
                "cold_baseline_cache_read_tokens": cold_baseline,
                "target_prefix_cache_read_tokens": (
                    max(int(r.get("cache_read_input_tokens") or 0) - cold_baseline, 0)
                    if cold_baseline is not None and _target_prefix_hit(r)
                    else None
                ),
                "target_prefix_evidence": _target_prefix_hit(r) if cold_baseline is not None else None,
                "total_logical_input_tokens": r.get("total_logical_input_tokens"),
                "cache_hit": bool(r.get("cache_hit")),
                "cache_hit_scope": r.get("cache_hit_scope"),
                "ttft_ms": r.get("ttft_ms"),
                "ttft_valid": bool(r.get("ttft_valid")),
                "total_ms": r.get("total_ms"),
                "warning": r.get("cache_metrics_warning"),
            }
            for r in items
        ],
    }
