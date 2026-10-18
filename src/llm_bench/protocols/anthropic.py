"""Anthropic Messages compatible protocol (streaming + usage)."""
from __future__ import annotations

from typing import Any

from llm_bench.protocols import Protocol, register, StreamMetrics
from llm_bench.transport.sse import SseStream

DEFAULT_MESSAGES_PATH = "/messages"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProtocol(Protocol):
    name = "anthropic"
    sse_label = "anthropic_messages_sse"

    @property
    def input_token_semantics(self) -> str:
        # Anthropic usage.input_tokens excludes cache_creation/cache_read tokens.
        return "excludes_cache"

    def headers(
        self, api_key: str, request_id: str, stream: bool, options: dict[str, Any]
    ) -> dict[str, str]:
        result = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "X-Client-Request-Id": request_id,
        }
        # Gateways differ: some want x-api-key only, some want a bearer token.
        if options.get("send_authorization_header", True):
            result["Authorization"] = f"Bearer {api_key}"
        if stream:
            result["Accept"] = "text/event-stream"
        return result

    def messages_path(self, options: dict[str, Any]) -> str:
        return str(options.get("messages_path", DEFAULT_MESSAGES_PATH))

    def messages_payload(
        self,
        model: str,
        prompt: str,
        *,
        max_tokens: int = 64,
        thinking_disabled: bool = False,
        system_blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if system_blocks is not None:
            payload["system"] = system_blocks
        if thinking_disabled:
            payload["thinking"] = {"type": "disabled"}
        return payload

    def cache_payload(
        self,
        model: str,
        prefix: str,
        suffix: str,
        *,
        mode: str,
        max_tokens: int = 8,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build the cache probe payload.

        mode=auto: the fixed prefix travels inside the user message.
        mode=anthropic_explicit: the prefix becomes a system block with an
        explicit cache_control breakpoint.
        """
        if mode == "anthropic_explicit":
            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "system": [{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": suffix}],
            }
        else:
            payload = self.messages_payload(model, f"{prefix}\n{suffix}", max_tokens=max_tokens)
        payload["stream"] = stream
        return payload

    def usage_fields(self, usage: dict[str, Any]) -> dict[str, Any]:
        gateway = {
            key: usage[key]
            for key in usage
            if key.startswith("claude_cache_creation") or key in ("gateway_cache_fields",)
        }
        return {
            "input_tokens_raw": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens") or 0,
            "cache_read_input_tokens": usage.get("cache_read_input_tokens") or 0,
            "thinking_tokens": 0,
            "cache_field_path": "usage.cache_read_input_tokens" if usage.get("cache_read_input_tokens") else None,
            "gateway_cache_fields": gateway,
        }

    def parse_stream(self, stream: SseStream) -> StreamMetrics:
        metrics = StreamMetrics()
        input_usage: dict[str, Any] = {}
        output_usage: dict[str, Any] = {}
        ttft_set = False
        for event in stream.events:
            if event.data is None:
                continue
            if metrics.first_event_ms is None:
                metrics.first_event_ms = event.elapsed_ms
            etype = event.data.get("type")
            if etype == "message_start":
                input_usage = (event.data.get("message") or {}).get("usage") or {}
            elif etype == "content_block_delta":
                delta = event.data.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "thinking_delta":
                    text = str(delta.get("thinking") or "")
                    metrics.reasoning_chars += len(text)
                    # Empty deltas must not start the TTFT clock; only a
                    # non-empty semantic delta counts.
                    if text:
                        if metrics.first_reasoning_ms is None:
                            metrics.first_reasoning_ms = event.elapsed_ms
                        if not ttft_set:
                            metrics.ttft_ms = event.elapsed_ms
                            metrics.ttft_source = "reasoning"
                            ttft_set = True
                elif dtype == "text_delta":
                    text = str(delta.get("text") or "")
                    metrics.content_chars += len(text)
                    if text:
                        if metrics.first_content_ms is None:
                            metrics.first_content_ms = event.elapsed_ms
                        if not ttft_set:
                            metrics.ttft_ms = event.elapsed_ms
                            metrics.ttft_source = "content"
                            ttft_set = True
            elif etype == "message_delta":
                output_usage = event.data.get("usage") or {}
        usage = {**input_usage, **{k: v for k, v in output_usage.items() if v is not None}}
        if usage:
            metrics.usage = usage
        if not ttft_set:
            metrics.missing_reason = "no_semantic_delta"
        return metrics


register(AnthropicProtocol())
