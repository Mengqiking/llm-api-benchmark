import unittest
from pathlib import Path

try:
    from tests.helpers import sse_pairs_from_fixture
except ImportError:  # pragma: no cover
    from helpers import sse_pairs_from_fixture  # type: ignore

from llm_bench.protocols import get_protocol
from llm_bench.transport.sse import parse_sse_lines

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sse"


class TestOpenAiStream(unittest.TestCase):
    def setUp(self):
        self.protocol = get_protocol("openai")

    def _parse(self, name):
        return parse_sse_lines(sse_pairs_from_fixture(FIXTURES / name))

    def test_role_first_event_has_no_content(self):
        metrics = self.protocol.parse_stream(self._parse("openai_role_first.txt"))
        # first data event at 100ms carries only role, no semantic delta yet
        self.assertEqual(metrics.first_event_ms, 100.0)
        self.assertEqual(metrics.ttft_ms, 200.0)
        self.assertEqual(metrics.ttft_source, "content")
        self.assertEqual(metrics.first_content_ms, 200.0)
        self.assertIsNone(metrics.first_reasoning_ms)
        self.assertTrue(metrics.ttft_valid)
        self.assertEqual(metrics.usage.get("prompt_tokens"), 12)

    def test_reasoning_arrives_before_content(self):
        metrics = self.protocol.parse_stream(self._parse("openai_reasoning_first.txt"))
        self.assertEqual(metrics.ttft_ms, 200.0)
        self.assertEqual(metrics.ttft_source, "reasoning")
        self.assertEqual(metrics.first_reasoning_ms, 200.0)
        self.assertEqual(metrics.first_content_ms, 400.0)
        self.assertGreater(metrics.reasoning_chars, 0)
        self.assertGreater(metrics.content_chars, 0)
        # usage tail package
        self.assertEqual(metrics.usage.get("prompt_tokens_details", {}).get("cached_tokens"), 8)

    def test_content_arrives_first(self):
        metrics = self.protocol.parse_stream(self._parse("openai_content_first.txt"))
        self.assertEqual(metrics.ttft_ms, 100.0)
        self.assertEqual(metrics.ttft_source, "content")
        self.assertEqual(metrics.first_content_ms, 100.0)
        self.assertIsNone(metrics.first_reasoning_ms)

    def test_usage_tail_and_done_sentinel(self):
        metrics = self.protocol.parse_stream(self._parse("openai_role_first.txt"))
        self.assertEqual(metrics.usage.get("completion_tokens"), 1)

    def test_two_hundred_without_semantic_delta_is_invalid(self):
        metrics = self.protocol.parse_stream(self._parse("openai_no_semantic.txt"))
        self.assertIsNone(metrics.ttft_ms)
        self.assertFalse(metrics.ttft_valid)
        self.assertEqual(metrics.missing_reason, "no_semantic_delta")
        self.assertEqual(metrics.first_event_ms, 100.0)

    def test_empty_content_delta_does_not_start_ttft(self):
        # an empty content delta arrives at 100ms, "OK" at 300ms:
        # TTFT must be 300ms, not the empty delta's arrival time
        pairs = [
            (100.0, 'data: {"choices": [{"delta": {"role": "assistant"}, "index": 0}]}'),
            (200.0, 'data: {"choices": [{"delta": {"content": ""}, "index": 0}]}'),
            (300.0, 'data: {"choices": [{"delta": {"content": "OK"}, "index": 0}]}'),
        ]
        metrics = self.protocol.parse_stream(parse_sse_lines(pairs))
        self.assertEqual(metrics.ttft_ms, 300.0)
        self.assertEqual(metrics.first_content_ms, 300.0)
        self.assertEqual(metrics.ttft_source, "content")
        self.assertTrue(metrics.ttft_valid)

    def test_all_content_deltas_empty_is_invalid(self):
        pairs = [
            (100.0, 'data: {"choices": [{"delta": {"content": ""}, "index": 0}]}'),
            (200.0, 'data: {"choices": [{"delta": {}, "index": 0}]}'),
        ]
        metrics = self.protocol.parse_stream(parse_sse_lines(pairs))
        self.assertFalse(metrics.ttft_valid)
        self.assertIsNone(metrics.ttft_ms)
        self.assertEqual(metrics.missing_reason, "no_semantic_delta")

    def test_empty_stream(self):
        metrics = self.protocol.parse_stream(parse_sse_lines([]))
        self.assertFalse(metrics.ttft_valid)
        self.assertEqual(metrics.missing_reason, "no_semantic_delta")


class TestAnthropicStream(unittest.TestCase):
    def setUp(self):
        self.protocol = get_protocol("anthropic")

    def test_basic_event_flow(self):
        metrics = self.protocol.parse_stream(self._parse())
        # first data event is message_start at 200ms, not a semantic delta
        self.assertEqual(metrics.first_event_ms, 200.0)
        # thinking_delta data at 600ms starts the TTFT clock
        self.assertEqual(metrics.ttft_ms, 600.0)
        self.assertEqual(metrics.ttft_source, "reasoning")
        self.assertEqual(metrics.first_reasoning_ms, 600.0)
        self.assertEqual(metrics.first_content_ms, 1200.0)
        self.assertTrue(metrics.ttft_valid)
        # merged usage: input from message_start, output from message_delta
        self.assertEqual(metrics.usage.get("input_tokens"), 1705)
        self.assertEqual(metrics.usage.get("output_tokens"), 8)
        self.assertEqual(metrics.usage.get("cache_read_input_tokens"), 64)

    def _parse(self):
        return parse_sse_lines(sse_pairs_from_fixture(FIXTURES / "anthropic_basic.txt"))

    def test_message_stop_only_stream(self):
        pairs = [(100.0, 'data: {"type": "message_stop"}')]
        metrics = self.protocol.parse_stream(parse_sse_lines(pairs))
        self.assertFalse(metrics.ttft_valid)
        self.assertEqual(metrics.missing_reason, "no_semantic_delta")

    def test_empty_text_delta_does_not_start_ttft(self):
        pairs = [
            (100.0, 'data: {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}'),
            (200.0, 'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": ""}}'),
            (300.0, 'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "OK"}}'),
        ]
        metrics = self.protocol.parse_stream(parse_sse_lines(pairs))
        # TTFT must use the non-empty delta at 300ms, not the empty one at 200ms
        self.assertEqual(metrics.ttft_ms, 300.0)
        self.assertEqual(metrics.first_content_ms, 300.0)
        self.assertEqual(metrics.ttft_source, "content")
        self.assertTrue(metrics.ttft_valid)

    def test_empty_thinking_delta_then_nonempty_uses_nonempty_time(self):
        pairs = [
            (100.0, 'data: {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}'),
            (200.0, 'data: {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": ""}}'),
            (500.0, 'data: {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "想"}}'),
        ]
        metrics = self.protocol.parse_stream(parse_sse_lines(pairs))
        self.assertEqual(metrics.ttft_ms, 500.0)
        self.assertEqual(metrics.first_reasoning_ms, 500.0)
        self.assertEqual(metrics.ttft_source, "reasoning")
        self.assertTrue(metrics.ttft_valid)

    def test_all_deltas_empty_is_invalid(self):
        pairs = [
            (100.0, 'data: {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}'),
            (200.0, 'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": ""}}'),
            (300.0, 'data: {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": ""}}'),
            (400.0, 'data: {"type": "message_stop"}'),
        ]
        metrics = self.protocol.parse_stream(parse_sse_lines(pairs))
        self.assertFalse(metrics.ttft_valid)
        self.assertIsNone(metrics.ttft_ms)
        self.assertEqual(metrics.missing_reason, "no_semantic_delta")


class TestSemantics(unittest.TestCase):
    def test_semantics_differ_per_protocol(self):
        self.assertEqual(get_protocol("openai").input_token_semantics, "includes_cache")
        self.assertEqual(get_protocol("anthropic").input_token_semantics, "excludes_cache")


if __name__ == "__main__":
    unittest.main()
