import json
import tempfile
import unittest
from pathlib import Path

from llm_bench.store import REDACTED, RunStore, make_run_paths, sanitize_headers, sanitize_record, sanitize_url

AUTH = "sk-test-not-a-real-key-do-not-scan-as-secret"


class TestHeaderSanitization(unittest.TestCase):
    def test_sensitive_headers_redacted(self):
        headers = {
            "Authorization": f"Bearer {AUTH}",
            "x-api-key": AUTH,
            "Cookie": "session=abc",
            "Set-Cookie": "sid=xyz",
            "Proxy-Authorization": "Basic abc",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        cleaned = sanitize_headers(headers)
        for key in ("Authorization", "x-api-key", "Cookie", "Set-Cookie", "Proxy-Authorization"):
            self.assertEqual(cleaned[key], REDACTED)
        self.assertEqual(cleaned["Content-Type"], "application/json")
        self.assertEqual(cleaned["anthropic-version"], "2023-06-01")

    def test_case_insensitive(self):
        self.assertEqual(sanitize_headers({"AUTHORIZATION": "x"})["AUTHORIZATION"], REDACTED)


class TestUrlSanitization(unittest.TestCase):
    def test_query_credentials_redacted(self):
        url = "https://gw.example/v1/chat/completions?api_key=abc&token=xyz&access_token=q&max_tokens=100"
        cleaned = sanitize_url(url)
        self.assertIn("max_tokens=100", cleaned)
        self.assertNotIn("abc", cleaned)
        self.assertNotIn("xyz", cleaned)
        self.assertNotIn("=q", cleaned)
        # urlencode percent-encodes the brackets of [REDACTED]
        self.assertEqual(cleaned.count("REDACTED"), 3)

    def test_url_without_query_unchanged(self):
        self.assertEqual(sanitize_url("https://gw.example/v1/models"), "https://gw.example/v1/models")


class TestRecordSanitization(unittest.TestCase):
    def test_record_deep_redaction(self):
        record = {
            "url": "https://gw.example/v1/messages?key=topsecret",
            "request_headers": {"Authorization": f"Bearer {AUTH}"},
            "response_headers": {"Set-Cookie": "sid=1"},
            "request_body": {"messages": [{"role": "user", "content": "hi"}]},
        }
        cleaned = sanitize_record(record)
        self.assertNotIn("topsecret", cleaned["url"])
        self.assertEqual(cleaned["request_headers"]["Authorization"], REDACTED)
        self.assertEqual(cleaned["response_headers"]["Set-Cookie"], REDACTED)
        # original untouched
        self.assertEqual(record["request_headers"]["Authorization"], f"Bearer {AUTH}")


class TestRunStorePersistence(unittest.TestCase):
    def test_persisted_records_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_run_paths(Path(tmp), "tostco", "kimi-k3")
            store = RunStore(paths)
            record = {
                "request_id": "req_1",
                "url": "https://gw.example/v1/messages",
                "request_headers": {"x-api-key": AUTH, "Content-Type": "application/json"},
                "response_headers": {},
            }
            store.save(record)
            persisted = json.loads(
                (paths.raw_requests / "req_1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["request_headers"]["x-api-key"], REDACTED)
            self.assertNotIn(AUTH, json.dumps(persisted))

    def test_nested_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = make_run_paths(root, "tostco", "kimi-k3")
            self.assertTrue(paths.root.is_dir())
            self.assertEqual(paths.root.parents[2], root)
            self.assertEqual(paths.root.parents[1].name, "tostco")

    def test_flat_layout_for_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = make_run_paths(root, "tostco", "kimi-k3", layout="flat")
            self.assertEqual(paths.root.parent, root)


if __name__ == "__main__":
    unittest.main()
