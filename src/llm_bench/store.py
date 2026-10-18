"""Run persistence: directory layout, sanitized raw records, manifest.

Sensitive-data rules (enforced by the sanitize helpers below):
- headers Authorization / x-api-key / Cookie / Set-Cookie / Proxy-Authorization
  are replaced with [REDACTED] before anything is written to disk;
- URL query parameters named like key/token/access_token are redacted.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from llm_bench import SCHEMA_VERSION

REDACTED = "[REDACTED]"

SENSITIVE_HEADERS = {
    "authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
    "proxy-authorization",
}

# Query parameter names that carry credentials, matched case-insensitively.
# Endswith matching avoids redacting innocuous params such as max_tokens.
_SENSITIVE_QUERY_SUFFIXES = ("key", "token", "secret", "signature", "auth")
_SENSITIVE_QUERY_EXACT = {"access_token", "apikey", "api-key", "api_key", "access-key", "accesskey"}


def is_sensitive_header(name: str) -> bool:
    return name.lower() in SENSITIVE_HEADERS


def sanitize_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {k: (REDACTED if is_sensitive_header(k) else v) for k, v in headers.items()}


def _query_param_is_sensitive(name: str) -> bool:
    lowered = name.lower()
    if lowered in _SENSITIVE_QUERY_EXACT:
        return True
    return lowered.endswith(_SENSITIVE_QUERY_SUFFIXES)


def sanitize_url(url: str) -> str:
    if "?" not in url:
        return url
    parts = urlsplit(url)
    query = [
        (k, REDACTED if _query_param_is_sensitive(k) else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-enough copy of a raw record with secrets redacted."""
    cleaned = dict(record)
    cleaned["url"] = sanitize_url(str(record.get("url") or ""))
    cleaned["request_headers"] = sanitize_headers(record.get("request_headers"))
    cleaned["response_headers"] = sanitize_headers(record.get("response_headers"))
    return cleaned


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


@dataclass
class RunPaths:
    root: Path
    raw_requests: Path
    normalized: Path
    report: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"


def make_run_paths(output_root: Path, provider_id: str, model: str, *, layout: str = "nested") -> RunPaths:
    """layout=nested -> <root>/<provider>/<model>/<timestamp>/ (new runs)
    layout=flat   -> <root>/<timestamp>/                (legacy-compatible)"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if layout == "nested":
        run_root = output_root / provider_id / _safe_name(model) / stamp
    elif layout == "flat":
        run_root = output_root / stamp
    else:
        raise ValueError(f"unknown layout: {layout!r}")
    paths = RunPaths(
        root=run_root,
        raw_requests=run_root / "raw" / "requests",
        normalized=run_root / "normalized",
        report=run_root / "report",
    )
    for path in (paths.raw_requests, paths.normalized, paths.report):
        path.mkdir(parents=True, exist_ok=False)
    return paths


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


class RunStore:
    """Persists sanitized raw records and in-memory copies for normalization."""

    def __init__(self, paths: RunPaths) -> None:
        self.paths = paths
        self._records: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def save(self, record: dict[str, Any]) -> None:
        cleaned = sanitize_record(record)
        (self.paths.raw_requests / f"{cleaned['request_id']}.json").write_text(
            json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with self._lock:
            self._records.append(cleaned)

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        self.paths.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def manifest_base(self, **fields: Any) -> dict[str, Any]:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "tool_version": fields.pop("tool_version"),
            "provider": fields.pop("provider"),
            "model": fields.pop("model"),
            "profile": fields.pop("profile"),
            "protocols": fields.pop("protocols"),
            "started_at": fields.pop("started_at", now_iso()),
            "python": fields.pop("python"),
            "platform": fields.pop("platform"),
            "params": fields.pop("params"),
            "network_requests_executed": fields.pop("network_requests_executed", False),
            "secrets_persisted": False,
        }
        manifest.update(fields)
        return manifest
