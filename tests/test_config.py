import json
import tempfile
import unittest
from pathlib import Path

try:
    from tests.helpers import sse_pairs_from_fixture  # noqa: F401
except ImportError:  # pragma: no cover
    from helpers import sse_pairs_from_fixture  # type: ignore  # noqa: F401

TOOLKIT = Path(__file__).resolve().parents[1]
PROVIDERS = TOOLKIT / "providers"
PROFILES = TOOLKIT / "profiles"

from llm_bench.config import (  # noqa: E402
    ConfigError,
    load_profile,
    load_provider,
    profile_from_dict,
    provider_from_dict,
    resolve,
)


class TestProviderLoading(unittest.TestCase):
    def test_load_builtin_providers(self):
        for provider_id in ("tostco", "tokenstars", "openjuly"):
            provider = load_provider(provider_id, PROVIDERS)
            self.assertEqual(provider.id, provider_id)
            self.assertTrue(provider.base_url.startswith("https://"))
            self.assertTrue(provider.api_key_env)
            for protocol in provider.protocols:
                self.assertIn(
                    provider.semantics_for(protocol),
                    ("excludes_cache", "includes_cache", "unknown"),
                )

    def test_default_profiles_cache_stream_true(self):
        # streaming cache probes are the default: only they yield cache TTFT
        for profile_id in ("smoke", "standard", "cache"):
            profile = load_profile(profile_id, PROFILES)
            self.assertTrue(profile.cache.stream, profile_id)

    def test_missing_provider_gives_readable_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_provider("nope", PROVIDERS)
        message = str(ctx.exception)
        self.assertIn("nope", message)
        self.assertIn("可用的 provider", message)
        self.assertIn("tostco", message)

    def test_missing_required_field(self):
        data = json.loads((PROVIDERS / "tostco.json").read_text(encoding="utf-8"))
        del data["api_key_env"]
        with self.assertRaises(ConfigError) as ctx:
            provider_from_dict(data, "test provider")
        self.assertIn("api_key_env", str(ctx.exception))

    def test_invalid_protocol(self):
        data = json.loads((PROVIDERS / "tostco.json").read_text(encoding="utf-8"))
        data["protocols"] = ["grpc"]
        with self.assertRaises(ConfigError) as ctx:
            provider_from_dict(data, "test provider")
        self.assertIn("protocols", str(ctx.exception))

    def test_invalid_cache_semantics(self):
        data = json.loads((PROVIDERS / "tostco.json").read_text(encoding="utf-8"))
        data["input_token_semantics"] = {"anthropic": "magic"}
        with self.assertRaises(ConfigError) as ctx:
            provider_from_dict(data, "test provider")
        self.assertIn("input_token_semantics", str(ctx.exception))

    def test_invalid_base_url(self):
        data = json.loads((PROVIDERS / "tostco.json").read_text(encoding="utf-8"))
        data["base_url"] = "ftp://x"
        with self.assertRaises(ConfigError):
            provider_from_dict(data, "test provider")

    def test_unknown_field_rejected(self):
        data = json.loads((PROVIDERS / "tostco.json").read_text(encoding="utf-8"))
        data["real_api_key"] = "should never be allowed"
        with self.assertRaises(ConfigError) as ctx:
            provider_from_dict(data, "test provider")
        self.assertIn("real_api_key", str(ctx.exception))

    def test_invalid_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_provider("broken", Path(tmp))


class TestProfileLoading(unittest.TestCase):
    def test_load_builtin_profiles(self):
        for profile_id in ("smoke", "standard", "cache"):
            profile = load_profile(profile_id, PROFILES)
            self.assertTrue(profile.suites)
            self.assertGreater(profile.timeout_seconds, 0)
            self.assertGreaterEqual(profile.cache.total_requests, 2)

    def test_missing_profile_gives_readable_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_profile("nope", PROFILES)
        self.assertIn("nope", str(ctx.exception))

    def test_invalid_suite(self):
        data = json.loads((PROFILES / "standard.json").read_text(encoding="utf-8"))
        data["suites"] = ["perf"]
        with self.assertRaises(ConfigError) as ctx:
            profile_from_dict(data, "test profile")
        self.assertIn("suites", str(ctx.exception))

    def test_non_positive_int_rejected(self):
        data = json.loads((PROFILES / "standard.json").read_text(encoding="utf-8"))
        data["timeout_seconds"] = 0
        with self.assertRaises(ConfigError):
            profile_from_dict(data, "test profile")

    def test_input_sizes_must_be_positive_ints(self):
        data = json.loads((PROFILES / "standard.json").read_text(encoding="utf-8"))
        data["input_sizes"] = [1024, -5]
        with self.assertRaises(ConfigError) as ctx:
            profile_from_dict(data, "test profile")
        self.assertIn("input_sizes", str(ctx.exception))

    def test_cache_total_mismatch(self):
        data = json.loads((PROFILES / "standard.json").read_text(encoding="utf-8"))
        data["cache"] = {"cold_start_requests": 1, "warm_requests": 2, "total_requests": 9}
        with self.assertRaises(ConfigError) as ctx:
            profile_from_dict(data, "test profile")
        self.assertIn("total_requests", str(ctx.exception))

    def test_invalid_cache_mode(self):
        data = json.loads((PROFILES / "standard.json").read_text(encoding="utf-8"))
        data["cache"] = {"mode": "telepathy"}
        with self.assertRaises(ConfigError):
            profile_from_dict(data, "test profile")


class TestResolve(unittest.TestCase):
    def _provider(self, **overrides):
        data = json.loads((PROVIDERS / "tostco.json").read_text(encoding="utf-8"))
        data.update(overrides)
        return provider_from_dict(data, "test provider")

    def _profile(self, **overrides):
        data = json.loads((PROFILES / "standard.json").read_text(encoding="utf-8"))
        data.update(overrides)
        return profile_from_dict(data, "test profile")

    def test_resolve_ok_and_model_default(self):
        provider = self._provider()
        profile = self._profile()
        resolved = resolve(provider, None, profile)
        self.assertEqual(resolved.model, provider.default_model)
        resolved2 = resolve(provider, "other-model", profile)
        self.assertEqual(resolved2.model, "other-model")

    def test_explicit_cache_requires_anthropic(self):
        provider = self._provider(protocols=["openai"])
        profile = self._profile(cache={
            "mode": "anthropic_explicit", "cold_start_requests": 1, "warm_requests": 2,
        })
        with self.assertRaises(ConfigError) as ctx:
            resolve(provider, None, profile)
        self.assertIn("anthropic", str(ctx.exception))

    def test_reasoning_requires_openai(self):
        provider = self._provider(protocols=["anthropic"])
        profile = self._profile(suites=["reasoning"])
        with self.assertRaises(ConfigError) as ctx:
            resolve(provider, None, profile)
        self.assertIn("openai", str(ctx.exception))

    def test_cache_requires_supported_protocol(self):
        provider = provider_from_dict(
            {
                "id": "x", "display_name": "X", "base_url": "https://x.example/v1",
                "default_model": "m", "protocols": [], "api_key_env": "X_KEY",
            },
            "x",
        )
        with self.assertRaises(ConfigError):
            resolve(provider, None, self._profile(suites=["cache"]))


if __name__ == "__main__":
    unittest.main()
