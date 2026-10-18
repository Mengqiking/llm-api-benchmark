"""OpenAI Chat Completions compatible protocol (streaming + usage)."""
from __future__ import annotations

from typing import Any

from llm_bench.protocols import Protocol, register, StreamMetrics
from llm_bench.transport.sse import SseStream

DEFAULT_CHAT_PATH = "/chat/completions"
DEFAULT_MODELS_PATH = "/models"


class OpenAIProtocol(Protocol):
    name = "openai"
    sse_label = "openai_chat_sse"

    @property
    def input_token_semantics(self) -> str:
        # OpenAI reports prompt_tokens including cached_tokens.
        return "includes_cache"

    def headers(
        self, api_key: str, request_id: str, stream: bool, options: dict[str, Any]
    ) -> dict[str, str]:
        result = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-Client-Request-Id": request_id,
        }
        if stream:
            result["Accept"] = "text/event-stream"
        return result

    def chat_path(self, options: dict[str, Any]) -> str:
        return str(options.get("chat_path", DEFAULT_CHAT_PATH))

    def models_path(self, options: dict[str, Any]) -> str:
        return str(options.get("models_path", DEFAULT_MODELS_PATH))

    def chat_payload(
        self,
        model: str,
        prompt: str,
        *,
        max_tokens: int = 64,
        effort: str = "low",
        stream: bool = True,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = options or {}
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 1.0,
            "stream": stream,
        }
        max_field = str(options.get("max_tokens_field", "max_completion_tokens"))
        payload[max_field] = max_tokens
        if options.get("include_reasoning_effort", True):
            payload["reasoning_effort"] = effort
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def video_payload(self, model: str, video_url: str, max_tokens: int) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请说明视频中的主要角色和一个可见动作，限制20字。"},
                        {"type": "video_url", "video_url": {"url": video_url}},
                    ],
                }
            ],
            "max_completion_tokens": max_tokens,
            "temperature": 1.0,
            "stream": False,
        }

    def usage_fields(self, usage: dict[str, Any]) -> dict[str, Any]:
        prompt_details = usage.get("prompt_tokens_details") or {}
        details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
        return {
            "input_tokens_raw": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens") or 0,
            "cache_read_input_tokens": (
                usage.get("cache_read_input_tokens") or prompt_details.get("cached_tokens") or 0
            ),
            "thinking_tokens": details.get("thinking_tokens") or details.get("reasoning_tokens") or 0,
            "cache_field_path": (
                "usage.prompt_tokens_details.cached_tokens"
                if prompt_details.get("cached_tokens")
                else None
            ),
        }

    def parse_stream(self, stream: SseStream) -> StreamMetrics:
        metrics = StreamMetrics()
        usage: dict[str, Any] | None = None
        ttft_set = False
        for event in stream.events:
            if event.data is None:
                continue
            if metrics.first_event_ms is None:
                metrics.first_event_ms = event.elapsed_ms
            if event.data.get("usage"):
                usage = event.data["usage"]
            for choice in event.data.get("choices") or []:
                delta = choice.get("delta") or {}
                reasoning = str(delta.get("reasoning_content") or delta.get("reasoning") or "")
                content = str(delta.get("content") or "")
                if reasoning:
                    metrics.reasoning_chars += len(reasoning)
                    if metrics.first_reasoning_ms is None:
                        metrics.first_reasoning_ms = event.elapsed_ms
                if content:
                    metrics.content_chars += len(content)
                    if metrics.first_content_ms is None:
                        metrics.first_content_ms = event.elapsed_ms
                if not ttft_set and (reasoning or content):
                    metrics.ttft_ms = event.elapsed_ms
                    metrics.ttft_source = "reasoning" if reasoning else "content"
                    ttft_set = True
        if usage:
            metrics.usage = usage
        if not ttft_set:
            metrics.missing_reason = "no_semantic_delta"
        return metrics


register(OpenAIProtocol())
