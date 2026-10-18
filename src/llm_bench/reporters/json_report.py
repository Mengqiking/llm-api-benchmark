"""Build the canonical summary dict. Markdown and JSON reports both render
from this structure so the numbers can never diverge."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from llm_bench import SCHEMA_VERSION
from llm_bench.metrics.cache import summarize_cache
from llm_bench.metrics.latency import is_ttft_eligible, percentile_nearest_rank, summarize_ttft


def _suite_status(rows: list[dict[str, Any]]) -> dict[str, str]:
    status: dict[str, str] = {}
    by_suite: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_suite.setdefault(str(row.get("suite")), []).append(row)
    for suite, subset in sorted(by_suite.items()):
        ok = sum(1 for r in subset if r.get("success"))
        status[suite] = f"{ok}/{len(subset)} 成功"
    return status


def _reasoning_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    efforts: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("reasoning_effort"):
            efforts.setdefault(str(row["reasoning_effort"]), []).append(row)
    for effort, subset in sorted(efforts.items()):
        ok = [r for r in subset if r.get("success")]
        ttft = [float(r["ttft_ms"]) for r in ok if r.get("ttft_valid") and r.get("ttft_ms") is not None]
        totals = [float(r["total_ms"]) for r in ok if r.get("total_ms") is not None]
        thinking = [float(r.get("thinking_tokens") or 0) for r in ok]
        out.append(
            {
                "effort": effort,
                "attempted": len(subset),
                "successful": len(ok),
                "ttft_p50_ms": percentile_nearest_rank(ttft, 50),
                "ttft_p95_ms": percentile_nearest_rank(ttft, 95),
                "total_p50_ms": percentile_nearest_rank(totals, 50),
                "thinking_tokens_median": percentile_nearest_rank(thinking, 50),
                "reasoning_present_rate": (
                    sum(1 for r in ok if r.get("reasoning_chars") or r.get("thinking_tokens")) / len(ok)
                    if ok
                    else None
                ),
            }
        )
    return out


def _reasoning_switch_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    off = [r for r in rows if r.get("test_name") == "reasoning_switch_off"]
    positive = [
        r for r in rows
        if r.get("suite") == "reasoning" and r.get("reasoning_effort") not in (None, "none")
    ]
    if not off:
        return {"tested": False, "supported": None, "status": "未测试"}

    off_ok = [r for r in off if r.get("success")]
    positive_ok = [r for r in positive if r.get("success")]
    off_observed = any(r.get("reasoning_chars") or r.get("thinking_tokens") for r in off_ok)
    positive_observed = any(r.get("reasoning_chars") or r.get("thinking_tokens") for r in positive_ok)

    if off_ok and positive_ok and not off_observed and positive_observed:
        supported: bool | None = True
        status = "支持，且输出侧观察到开关差异"
    elif off_ok and positive_ok:
        supported = None
        status = "reasoning_effort=none 与正向档位均被接受，但未观察到明确输出差异"
    elif not off_ok and positive_ok:
        supported = False
        status = "不支持通过 reasoning_effort=none 关闭"
    else:
        supported = None
        status = "无法判定"
    return {
        "tested": True,
        "supported": supported,
        "status": status,
        "off_attempted": len(off),
        "off_successful": len(off_ok),
        "positive_attempted": len(positive),
        "positive_successful": len(positive_ok),
        "off_reasoning_observed": off_observed,
        "positive_reasoning_observed": positive_observed,
    }


def build_summary(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    total = len(rows)
    success = sum(1 for r in rows if r.get("success"))

    # TTFT scope: only streamed benchmark requests (reasoning/latency/streamed
    # cache). smoke/video are non-streaming and preflight is infrastructure -
    # none of them may appear as "missing TTFT" samples.
    eligible = [r for r in rows if is_ttft_eligible(r)]
    ttft = summarize_ttft(eligible, group_keys=["input_size_chars", "concurrency"])

    by_suite: dict[str, dict[str, Any]] = {}
    for suite in ("latency", "reasoning", "cache"):
        subset = [r for r in eligible if str(r.get("suite")) == suite]
        if subset:
            by_suite[suite] = summarize_ttft(subset)
    ttft["by_suite"] = by_suite

    # Core TTFT prefers the latency suite (controlled input sizes/concurrency);
    # without latency data, fall back to all eligible benchmark requests.
    core_source = by_suite.get("latency") or ttft
    ttft_scope = "latency" if by_suite.get("latency") else "eligible_all"

    # Global error rate covers all formal (non-preflight) requests; the TTFT
    # error rate only covers TTFT-eligible requests.
    formal = [r for r in rows if str(r.get("suite")) != "preflight"]
    formal_failures = sum(1 for r in formal if not r.get("success"))

    reasoning = _reasoning_summary(rows)
    reasoning_switch = _reasoning_switch_summary(rows)
    cache_rows = [r for r in rows if r.get("suite") == "cache"]
    cache = summarize_cache(rows) if cache_rows else None

    warnings = [
        f"TTFT 缺失 {count} 个（{reason}）" for reason, count in ttft.get("missing_reasons", {}).items()
    ]
    if cache:
        warnings.extend(cache.get("warnings") or [])

    core = {
        "ttft_p50_ms": core_source["ttft_p50_ms"],
        "ttft_p95_ms": core_source["ttft_p95_ms"],
        "ttft_valid_samples": core_source["valid_samples"],
        "ttft_missing_samples": core_source["missing_samples"],
        "ttft_scope": ttft_scope,
        "post_cold_request_hit_rate": (cache or {}).get("post_cold", {}).get("request_hit_rate"),
        "post_cold_token_hit_rate": (cache or {}).get("post_cold", {}).get("token_hit_rate"),
        "error_rate": (formal_failures / len(formal)) if formal else None,
        "ttft_error_rate": ttft["error_rate"],
        "requests": total,
        "success": success,
        "failure": total - success,
    }
    core["global_error_rate"] = core["error_rate"]

    failures = [
        {
            "test_name": r.get("test_name"),
            "http_status": r.get("http_status"),
            "error": (r.get("error") or "")[:500],
        }
        for r in rows
        if not r.get("success")
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
        "manifest": manifest,
        "core": core,
        "ttft": ttft,
        "reasoning": reasoning,
        "reasoning_switch": reasoning_switch,
        "cache": cache,
        "suite_status": _suite_status(rows),
        "failures": failures,
        "warnings": warnings,
    }
