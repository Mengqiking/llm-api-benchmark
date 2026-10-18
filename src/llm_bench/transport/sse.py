"""Server-Sent Events parsing helpers (protocol-agnostic)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SseEvent:
    elapsed_ms: float
    raw_line: str
    data: Any = None          # parsed JSON payload when possible
    is_done: bool = False     # the [DONE] sentinel
    parse_error: bool = False


@dataclass
class SseStream:
    events: list[SseEvent] = field(default_factory=list)

    @property
    def first_event(self) -> SseEvent | None:
        return self.events[0] if self.events else None

    def json_events(self) -> list[SseEvent]:
        return [e for e in self.events if e.data is not None]


def parse_sse_lines(pairs: list[tuple[float, str]]) -> SseStream:
    """Parse raw SSE lines (excluding blank lines) into SseEvents.

    Timing values are taken verbatim from the transport, so tests can feed
    synthetic timings instead of sleeping.
    """
    stream = SseStream()
    for elapsed_ms, line in pairs:
        event = SseEvent(elapsed_ms=elapsed_ms, raw_line=line)
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                event.is_done = True
            else:
                try:
                    event.data = json.loads(payload)
                except json.JSONDecodeError:
                    event.parse_error = True
        stream.events.append(event)
    return stream
