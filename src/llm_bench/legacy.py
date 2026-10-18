"""Adapter for historical run directories produced by the generic scripts
(schema v1/v2/v3: raw/manifest.json + raw/requests/*.json + cleaned/*).

Supported layouts (read-only; the original directory is never modified):
1. raw/manifest.json + raw/requests/*.json   (preferred, full fidelity)
2. cleaned/requests.json only                (limited: no SSE/TTFT detail)

Token semantics for legacy data are UNKNOWN by default: legacy manifests never
recorded cache field semantics, and guessing from the protocol name alone is
not allowed. Token cache hit rates stay N/A until the caller either matches a
provider config by unique evidence (--provider) or passes an explicit override
(--input-token-semantics <protocol>=<semantics>).
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from llm_bench.models import BenchError, ProviderConfig, ResolvedConfig, TestProfile, CacheProfile
from llm_bench.runner import TEST_SUITE, Runner
from llm_bench.store import sanitize_record


class LegacyRunError(BenchError):
    pass


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_legacy_manifest(run_dir: Path) -> dict[str, Any] | None:
    for candidate in (run_dir / "manifest.json", run_dir / "raw" / "manifest.json"):
        if candidate.is_file():
            return _read_json(candidate)
    return None


def load_legacy_records(run_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """Returns (records, source) where source is 'raw' or 'cleaned'."""
    raw_dir = run_dir / "raw" / "requests"
    if raw_dir.is_dir():
        records = []
        for path in sorted(raw_dir.glob("*.json")):
            record = _read_json(path)
            if isinstance(record, dict):
                records.append(sanitize_record(record))
        if records:
            return records, "raw"
    cleaned = run_dir / "cleaned" / "requests.json"
    if cleaned.is_file():
        rows = _read_json(cleaned)
        if not isinstance(rows, list):
            raise LegacyRunError(f"{cleaned} 应为 JSON 数组")
        return _records_from_cleaned(rows), "cleaned"
    raise LegacyRunError(
        f"{run_dir} 既没有 raw/requests/*.json 也没有 cleaned/requests.json，无法识别为历史结果目录"
    )


def _records_from_cleaned(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild pseudo raw records from cleaned rows (limited fidelity)."""
    records = []
    for row in rows:
        metadata = {
            key: row[key]
            for key in ("input_size_chars", "concurrency", "reasoning_effort", "sequence")
            if row.get(key) is not None
        }
        usage: dict[str, Any] = {}
        if row.get("input_tokens") is not None:
            usage["input_tokens"] = row["input_tokens"]
        if row.get("output_tokens") is not None:
            usage["output_tokens"] = row["output_tokens"]
        if row.get("cache_creation_input_tokens"):
            usage["cache_creation_input_tokens"] = row["cache_creation_input_tokens"]
        if row.get("cache_read_input_tokens"):
            usage["cache_read_input_tokens"] = row["cache_read_input_tokens"]
        records.append(
            {
                "schema_version": 0,
                "request_id": row.get("request_id") or f"legacy_{len(records)}",
                "test_name": row.get("test_name") or "unknown",
                "protocol": row.get("protocol") or "openai",
                "started_at": row.get("started_at"),
                "completed_at": None,
                "method": "POST",
                "url": "",
                "request_headers": {},
                "request_body": None,
                "metadata": metadata,
                "http_status": row.get("http_status"),
                "success_hint": row.get("success"),
                "total_ms": row.get("total_ms"),
                "headers_ms": row.get("headers_ms"),
                "first_event_ms": row.get("first_event_ms"),
                "ttft_ms": row.get("ttft_ms"),
                "first_content_ms": row.get("first_content_ms"),
                "reasoning_chars": row.get("reasoning_chars") or 0,
                "content_chars": row.get("content_chars") or 0,
                "usage": usage,
                "error": row.get("error") or None,
                "cleaned_only": True,
            }
        )
    return records


UNKNOWN_LEGACY_SEMANTICS = {"anthropic": "unknown", "openai": "unknown"}


def legacy_provider_config(
    manifest: dict[str, Any] | None,
    run_dir: Path,
    provider: ProviderConfig | None = None,
    semantics_override: dict[str, str] | None = None,
) -> tuple[ProviderConfig, str]:
    """Build the provider used for re-analysis.

    Returns (provider, semantics_source) where semantics_source is one of
    "user_override", "provider_config", "unknown".
    """
    manifest = manifest or {}
    semantics_source = "unknown"
    if provider is None:
        protocols = [p for p in manifest.get("protocols") or [] if p in ("openai", "anthropic")] or ["openai"]
        provider = ProviderConfig(
            id=str(manifest.get("provider_name") or run_dir.name).lower().replace(" ", "-") or "legacy",
            display_name=str(manifest.get("provider_name") or run_dir.name),
            base_url=str(manifest.get("base_url") or "unknown"),
            default_model=str(manifest.get("model") or "unknown"),
            protocols=protocols,
            api_key_env="LEGACY_NOT_USED",
            input_token_semantics=dict(UNKNOWN_LEGACY_SEMANTICS),
        )
    else:
        if provider.input_token_semantics:
            semantics_source = "provider_config"
        else:
            provider = replace(provider, input_token_semantics=dict(UNKNOWN_LEGACY_SEMANTICS))
    if semantics_override:
        merged = dict(provider.input_token_semantics)
        merged.update(semantics_override)
        provider = replace(provider, input_token_semantics=merged)
        semantics_source = "user_override"
    return provider, semantics_source


def match_provider_from_manifest(
    manifest: dict[str, Any] | None, providers_dir: Path | None
) -> tuple[ProviderConfig | None, str]:
    """Find a provider config for a legacy run using explicit evidence only.

    Evidence types: manifest provider_name matching a config id/display_name,
    and manifest base_url matching a config base_url. A provider is returned
    only when exactly ONE config matches; ambiguous or missing evidence
    returns (None, "") and the caller must fall back to unknown semantics.
    """
    manifest = manifest or {}
    if providers_dir is None or not Path(providers_dir).is_dir():
        return None, ""
    from llm_bench.config import load_provider

    name = str(manifest.get("provider_name") or "").strip().lower()
    base_url = str(manifest.get("base_url") or "").strip().rstrip("/")
    matched: dict[str, list[str]] = {}
    for path in sorted(Path(providers_dir).glob("*.json")):
        candidate_id = path.stem
        evidence: list[str] = []
        try:
            candidate = load_provider(candidate_id, Path(providers_dir))
        except BenchError:
            continue
        if name and name in (candidate.id.lower(), candidate.display_name.strip().lower()):
            evidence.append(f"provider_name={manifest.get('provider_name')}")
        if base_url and candidate.base_url.rstrip("/") == base_url:
            evidence.append(f"base_url={base_url}")
        if evidence:
            matched[candidate_id] = evidence
    if len(matched) != 1:
        return None, ""
    provider_id, evidence = next(iter(matched.items()))
    return load_provider(provider_id, Path(providers_dir)), "、".join(evidence)


def _canonical_protocol(record: dict[str, Any]) -> None:
    """Legacy layouts sometimes stored SSE labels ('openai_chat_sse') as the
    protocol; map everything back to the canonical protocol names."""
    raw = str(record.get("protocol") or "")
    if raw in ("openai", "anthropic"):
        return
    if "anthropic" in raw:
        record["protocol"] = "anthropic"
    elif "openai" in raw:
        record["protocol"] = "openai"


def adapt_legacy_record(record: dict[str, Any]) -> None:
    """Fill neutral TTFT/usage fields on a legacy raw record in place."""
    _canonical_protocol(record)
    if not record.get("usage"):
        parsed = record.get("parsed_response")
        if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
            record["usage"] = parsed["usage"]
    events = record.get("raw_sse_events")
    if events:
        # Legacy SSE events carry (elapsed_ms, line); re-derive exact TTFT offline.
        pairs = [
            (float(e.get("elapsed_ms") or 0.0), str(e.get("line") or ""))
            for e in events
            if isinstance(e, dict)
        ]
        metrics = get_protocol(str(record.get("protocol") or "openai")).parse_stream(parse_sse_lines(pairs))
        record["first_event_ms"] = metrics.first_event_ms
        record["first_reasoning_ms"] = metrics.first_reasoning_ms
        record["first_content_ms"] = metrics.first_content_ms
        record["ttft_ms"] = metrics.ttft_ms
        record["ttft_source"] = metrics.ttft_source
        record["ttft_valid"] = metrics.ttft_valid
        record["ttft_missing_reason"] = None if metrics.ttft_valid else (
            metrics.missing_reason or "no_semantic_delta"
        )
        if not record.get("usage") and metrics.usage:
            record["usage"] = metrics.usage
        record.setdefault("request_started_at", record.get("started_at"))
        record.setdefault("suite", TEST_SUITE.get(str(record.get("test_name")), "unknown"))
        return
    if record.get("ttft_ms") is not None:
        record["ttft_valid"] = True
        record.setdefault("ttft_source", "unknown")
        record["ttft_missing_reason"] = None
    else:
        record["ttft_valid"] = False
        if record.get("error"):
            record["ttft_missing_reason"] = (
                "http_error" if record.get("http_status") is not None else "transport_error"
            )
        else:
            record["ttft_missing_reason"] = "non_streaming_request"
    record.setdefault("request_started_at", record.get("started_at"))
    record.setdefault("suite", TEST_SUITE.get(str(record.get("test_name")), "unknown"))


def reanalyze_run(
    run_dir: Path,
    output_dir: Path,
    *,
    provider: ProviderConfig | None = None,
    provider_evidence: str = "",
    semantics_override: dict[str, str] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Re-analyze a historical run directory without any network access.

    Token semantics resolution order: explicit override (--input-token-semantics)
    > matched provider config (--provider / unique manifest evidence) > unknown.
    With unknown semantics no token cache hit rate is produced.

    Writes a fresh toolkit-layout directory under output_dir and returns
    (summary, output_run_dir)."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise LegacyRunError(f"历史结果目录不存在：{run_dir}")
    manifest = load_legacy_manifest(run_dir)
    records, source = load_legacy_records(run_dir)
    for record in records:
        adapt_legacy_record(record)

    resolved_provider, semantics_source = legacy_provider_config(
        manifest, run_dir, provider=provider, semantics_override=semantics_override
    )
    resolved = ResolvedConfig(
        provider=resolved_provider,
        model=str((manifest or {}).get("model") or "unknown"),
        profile=TestProfile(
            suites=[],
            timeout_seconds=180,
            input_sizes=[],
            concurrency_levels=[],
            samples_per_group=1,
            reasoning_samples=1,
            max_output_tokens=1,
            cache=CacheProfile(),
        ),
    )
    runner = Runner(resolved, transport=object(), run_id=run_dir.name)
    rows = runner.normalize(records)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_root = output_dir / run_dir.name
    counter = 1
    while out_root.exists():
        counter += 1
        out_root = output_dir / f"{run_dir.name}_{counter}"
    (out_root / "normalized").mkdir(parents=True)
    (out_root / "report").mkdir(parents=True)

    override_text = (
        "、".join(f"{k}={v}" for k, v in sorted((semantics_override or {}).items())) or ""
    )
    new_manifest = {
        "tool_version": f"legacy-adapter(reanalyzed from {run_dir})",
        "provider": resolved_provider.id,
        "provider_display_name": resolved_provider.display_name,
        "base_url": resolved_provider.base_url,
        "model": resolved.model,
        "profile": "reanalyzed",
        "protocols": resolved_provider.protocols,
        "started_at": (manifest or {}).get("started_at"),
        "completed_at": (manifest or {}).get("completed_at"),
        "python": (manifest or {}).get("python"),
        "platform": (manifest or {}).get("platform"),
        "params": {
            key: value
            for key, value in (manifest or {}).items()
            if key
            not in {
                "schema_version", "provider_name", "base_url", "model", "protocols",
                "started_at", "completed_at", "python", "platform", "secrets_persisted",
                "request_count", "success_count", "failure_count", "suite_status",
            }
        },
        "network_requests_executed": False,
        "secrets_persisted": False,
        "input_token_semantics": resolved_provider.input_token_semantics,
        "input_token_semantics_source": semantics_source,
        "input_token_semantics_override": semantics_override or None,
        "provider_evidence": provider_evidence or None,
        "legacy_source": str(run_dir),
        "legacy_source_kind": source,
        "legacy_manifest": manifest,
        "legacy_schema_version": (manifest or {}).get("schema_version"),
    }

    from llm_bench.reporters import build_summary, render_markdown, write_requests_csv, write_requests_json

    write_requests_json(out_root / "normalized" / "requests.json", rows)
    write_requests_csv(out_root / "normalized" / "requests.csv", rows)
    summary = build_summary(rows, new_manifest)
    warnings: list[str] = []
    if semantics_source == "user_override":
        warnings.append(
            f"缓存字段语义来自用户覆盖（{override_text}）：命中率公式随覆盖值改变，"
            "请确认该覆盖有供应商数据依据"
        )
    elif semantics_source == "provider_config":
        warnings.append(
            f"缓存字段语义取自 provider 配置（{resolved_provider.id}）："
            "历史运行本身未记录语义，若该配置无数据依据请用 --input-token-semantics 覆盖"
        )
    else:
        warnings.append(
            "历史数据未记录缓存字段语义，且未指定可信 provider 配置或覆盖值："
            "input_token_semantics 视为 unknown，Token 缓存命中率为 N/A。"
            "如可确认语义，请使用 --provider 或 --input-token-semantics <protocol>=<semantics> 显式指定"
        )
    summary["warnings"][0:0] = warnings
    (out_root / "normalized" / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_root / "report" / "benchmark_report.md").write_text(render_markdown(summary), encoding="utf-8")
    (out_root / "manifest.json").write_text(
        json.dumps(new_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary, out_root
