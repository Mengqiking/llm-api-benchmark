"""HTTP transport layer.

`UrllibTransport` performs real network IO (only used by `run`).
`FakeTransport` replays scripted responses and is used by tests/dry paths so
that no network access ever happens.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TransportError(Exception):
    """Network-level failure outside the HTTP status space."""


@dataclass
class JsonOutcome:
    status: int | None
    headers: dict[str, str]
    raw_text: str
    headers_ms: float | None
    total_ms: float
    error: str | None = None

    @property
    def parsed(self) -> Any:
        if not self.raw_text:
            return None
        try:
            return json.loads(self.raw_text)
        except json.JSONDecodeError:
            return None


@dataclass
class StreamOutcome:
    status: int | None
    headers: dict[str, str]
    # (elapsed_ms, raw_line) pairs, in arrival order, excluding empty lines
    events: list[tuple[float, str]]
    headers_ms: float | None
    total_ms: float
    error: str | None = None
    raw_error_text: str = ""


class UrllibTransport:
    def __init__(self, timeout_seconds: int) -> None:
        self.timeout = timeout_seconds

    def json_request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> JsonOutcome:
        started = time.perf_counter()
        try:
            req = Request(url, data=body, headers=headers, method=method)
            with urlopen(req, timeout=self.timeout) as response:
                headers_ms = (time.perf_counter() - started) * 1000
                raw = response.read().decode("utf-8", errors="replace")
                return JsonOutcome(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    raw_text=raw,
                    headers_ms=round(headers_ms, 3),
                    total_ms=round((time.perf_counter() - started) * 1000, 3),
                )
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return JsonOutcome(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                raw_text=raw,
                headers_ms=None,
                total_ms=round((time.perf_counter() - started) * 1000, 3),
                error=f"HTTPError: {exc}",
            )
        except (URLError, TimeoutError, OSError) as exc:
            return JsonOutcome(
                status=None,
                headers={},
                raw_text="",
                headers_ms=None,
                total_ms=round((time.perf_counter() - started) * 1000, 3),
                error=f"{type(exc).__name__}: {exc}",
            )

    def stream(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> StreamOutcome:
        started = time.perf_counter()
        events: list[tuple[float, str]] = []
        headers_ms: float | None = None
        try:
            req = Request(url, data=body, headers=headers, method=method)
            with urlopen(req, timeout=self.timeout) as response:
                headers_ms = round((time.perf_counter() - started) * 1000, 3)
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    events.append((round((time.perf_counter() - started) * 1000, 3), line))
                return StreamOutcome(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    events=events,
                    headers_ms=headers_ms,
                    total_ms=round((time.perf_counter() - started) * 1000, 3),
                )
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return StreamOutcome(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                events=events,
                headers_ms=None,
                total_ms=round((time.perf_counter() - started) * 1000, 3),
                error=f"HTTPError: {exc}",
                raw_error_text=raw,
            )
        except (URLError, TimeoutError, OSError) as exc:
            return StreamOutcome(
                status=None,
                headers={},
                events=events,
                headers_ms=None,
                total_ms=round((time.perf_counter() - started) * 1000, 3),
                error=f"{type(exc).__name__}: {exc}",
            )


@dataclass
class FakeResponse:
    """Scripted response for one test_name; no network involved."""

    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""                                    # for json_request
    sse_events: list[tuple[float, str]] = field(default_factory=list)  # for stream
    total_ms: float = 123.0
    error: str | None = None
    raw_error_text: str = ""


class FakeTransport:
    """Deterministic offline transport. Responses are selected by test_name via
    `selector` (a callable receiving (test_name, method, url)) or exact test_name."""

    def __init__(
        self,
        responses: dict[str, FakeResponse] | FakeResponse | None = None,
        selector: Any = None,
    ) -> None:
        if isinstance(responses, FakeResponse):
            responses = {"*": responses}
        self.responses = responses or {}
        self.selector = selector
        self.calls: list[dict[str, Any]] = []
        self.request_id = staticmethod(lambda: uuid.uuid4().hex)

    def _pick(self, test_name: str, method: str, url: str) -> FakeResponse:
        self.calls.append({"test_name": test_name, "method": method, "url": url})
        if self.selector is not None:
            chosen = self.selector(test_name, method, url)
            if chosen is not None:
                return chosen
        if test_name in self.responses:
            return self.responses[test_name]
        if "*" in self.responses:
            return self.responses["*"]
        raise TransportError(f"FakeTransport has no scripted response for test_name={test_name!r}")

    def json_request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> JsonOutcome:
        test_name = headers.get("X-Client-Request-Id", "")
        fake = self._pick(test_name, method, url)
        return JsonOutcome(
            status=fake.status,
            headers=fake.headers,
            raw_text=fake.body,
            headers_ms=1.0,
            total_ms=fake.total_ms,
            error=fake.error,
        )

    def stream(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> StreamOutcome:
        test_name = headers.get("X-Client-Request-Id", "")
        fake = self._pick(test_name, method, url)
        return StreamOutcome(
            status=fake.status,
            headers=fake.headers,
            events=list(fake.sse_events),
            headers_ms=1.0,
            total_ms=fake.total_ms,
            error=fake.error,
            raw_error_text=fake.raw_error_text,
        )
