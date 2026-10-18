"""Provider/profile config loading, validation and resolution.

All user-facing validation failures raise ConfigError; the CLI maps them to
exit code 2. Real API keys must never appear in config files - only the *name*
of the environment variable that holds the key is configured here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm_bench.models import (
    CACHE_MODES,
    INPUT_TOKEN_SEMANTICS,
    SUPPORTED_PROTOCOLS,
    SUPPORTED_SUITES,
    BenchError,
    CacheProfile,
    ProviderConfig,
    ResolvedConfig,
    TestProfile,
)

PROVIDERS_SUBDIR = "providers"
PROFILES_SUBDIR = "profiles"


class ConfigError(BenchError):
    """Invalid or missing configuration (CLI exit code 2)."""


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: 缺少必填字段 `{key}`")
    return mapping[key]


def _check_type(value: Any, types: tuple[type, ...], key: str, where: str) -> Any:
    if not isinstance(value, types):
        names = "/".join(t.__name__ for t in types)
        raise ConfigError(f"{where}: 字段 `{key}` 应为 {names}，实际为 {type(value).__name__}")
    return value


def _check_positive_int(value: Any, key: str, where: str) -> int:
    _check_type(value, (int,), key, where)
    if isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{where}: 字段 `{key}` 必须为正整数，实际为 {value!r}")
    return value


def _check_choice(value: Any, choices: tuple[str, ...], key: str, where: str) -> str:
    if value not in choices:
        raise ConfigError(f"{where}: 字段 `{key}` 非法，允许值 {list(choices)}，实际为 {value!r}")
    return value


def _check_str_list(value: Any, key: str, where: str) -> list[str]:
    _check_type(value, (list,), key, where)
    for item in value:
        if not isinstance(item, str) or not item:
            raise ConfigError(f"{where}: 字段 `{key}` 必须为非空字符串列表，实际为 {value!r}")
    return list(dict.fromkeys(value))


def validate_provider(data: Any, where: str) -> dict[str, Any]:
    _check_type(data, (dict,), "root", where)
    out: dict[str, Any] = {}
    out["id"] = _check_type(_require(data, "id", where), (str,), "id", where)
    if not out["id"].strip():
        raise ConfigError(f"{where}: 字段 `id` 不能为空")
    out["display_name"] = _check_type(_require(data, "display_name", where), (str,), "display_name", where)
    base_url = _check_type(_require(data, "base_url", where), (str,), "base_url", where)
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{where}: 字段 `base_url` 必须以 http(s):// 开头，实际为 {base_url!r}")
    out["base_url"] = base_url.rstrip("/")
    out["default_model"] = _check_type(_require(data, "default_model", where), (str,), "default_model", where)
    protocols = _check_str_list(_require(data, "protocols", where), "protocols", where)
    for item in protocols:
        _check_choice(item, SUPPORTED_PROTOCOLS, "protocols", where)
    out["protocols"] = protocols
    out["api_key_env"] = _check_type(_require(data, "api_key_env", where), (str,), "api_key_env", where)

    extra_headers = data.get("extra_headers", {})
    _check_type(extra_headers, (dict,), "extra_headers", where)
    out["extra_headers"] = {str(k): str(v) for k, v in extra_headers.items()}

    protocol_options = data.get("protocol_options", {})
    _check_type(protocol_options, (dict,), "protocol_options", where)
    for key in protocol_options:
        _check_choice(key, SUPPORTED_PROTOCOLS, "protocol_options", where)
    out["protocol_options"] = protocol_options

    semantics = data.get("input_token_semantics", {})
    _check_type(semantics, (dict,), "input_token_semantics", where)
    checked: dict[str, str] = {}
    for key, value in semantics.items():
        _check_choice(key, SUPPORTED_PROTOCOLS, "input_token_semantics", where)
        checked[key] = _check_choice(value, INPUT_TOKEN_SEMANTICS, "input_token_semantics", where)
    out["input_token_semantics"] = checked

    out["cache_metric_source"] = _check_choice(
        data.get("cache_metric_source", "auto"),
        ("auto", "anthropic_usage", "openai_prompt_tokens_details", "gateway", "none"),
        "cache_metric_source",
        where,
    )
    gateway_fields = data.get("gateway_cache_fields", [])
    out["gateway_cache_fields"] = _check_str_list(gateway_fields, "gateway_cache_fields", where)

    unknown = sorted(set(data) - set(out))
    if unknown:
        raise ConfigError(f"{where}: 存在未知字段 {unknown}")
    return out


def validate_profile(data: Any, where: str) -> dict[str, Any]:
    _check_type(data, (dict,), "root", where)
    out: dict[str, Any] = {}
    suites = _check_str_list(_require(data, "suites", where), "suites", where)
    for item in suites:
        _check_choice(item, SUPPORTED_SUITES, "suites", where)
    out["suites"] = suites
    out["timeout_seconds"] = _check_positive_int(_require(data, "timeout_seconds", where), "timeout_seconds", where)
    for key in ("input_sizes", "concurrency_levels"):
        values = _check_str_list([str(v) for v in _require(data, key, where)], key, where)
        ints = []
        for raw in values:
            try:
                ints.append(int(raw))
            except ValueError as exc:
                raise ConfigError(f"{where}: 字段 `{key}` 必须为正整数列表，实际为 {data[key]!r}") from exc
            if ints[-1] <= 0:
                raise ConfigError(f"{where}: 字段 `{key}` 必须为正整数列表，实际为 {data[key]!r}")
        out[key] = ints
    for key in ("samples_per_group", "reasoning_samples", "max_output_tokens"):
        out[key] = _check_positive_int(_require(data, key, where), key, where)

    cache = data.get("cache", {})
    _check_type(cache, (dict,), "cache", where)
    cache_out: dict[str, Any] = {}
    cache_out["mode"] = _check_choice(cache.get("mode", "auto"), CACHE_MODES, "cache.mode", where)
    cache_out["stream"] = _check_type(cache.get("stream", True), (bool,), "cache.stream", where)
    cache_out["cold_start_requests"] = _check_positive_int(
        cache.get("cold_start_requests", 1), "cache.cold_start_requests", where
    )
    cache_out["warm_requests"] = _check_positive_int(
        cache.get("warm_requests", 4), "cache.warm_requests", where
    )
    if "total_requests" in cache:
        total = _check_positive_int(cache["total_requests"], "cache.total_requests", where)
        if total != cache_out["cold_start_requests"] + cache_out["warm_requests"]:
            raise ConfigError(
                f"{where}: cache.total_requests({total}) 必须等于 "
                f"cold_start_requests({cache_out['cold_start_requests']}) + warm_requests({cache_out['warm_requests']})"
            )
    cache_out["unique_prefix_per_run"] = _check_type(
        cache.get("unique_prefix_per_run", True), (bool,), "cache.unique_prefix_per_run", where
    )
    cache_out["varying_suffix"] = _check_type(
        cache.get("varying_suffix", True), (bool,), "cache.varying_suffix", where
    )
    unknown_cache = sorted(set(cache) - set(cache_out))
    if unknown_cache:
        raise ConfigError(f"{where}: cache 存在未知字段 {unknown_cache}")
    out["cache"] = cache_out

    unknown = sorted(set(data) - set(out))
    if unknown:
        raise ConfigError(f"{where}: 存在未知字段 {unknown}")
    return out


def provider_from_dict(data: dict[str, Any], where: str = "provider") -> ProviderConfig:
    return ProviderConfig(**validate_provider(data, where))


def profile_from_dict(data: dict[str, Any], where: str = "profile") -> TestProfile:
    validated = validate_profile(data, where)
    cache = CacheProfile(**validated["cache"])
    validated.pop("cache")
    return TestProfile(cache=cache, **validated)


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法 JSON：{exc}") from exc


def load_provider(provider_id: str, providers_dir: Path) -> ProviderConfig:
    path = providers_dir / f"{provider_id}.json"
    if not path.is_file():
        available = sorted(p.stem for p in providers_dir.glob("*.json")) if providers_dir.is_dir() else []
        raise ConfigError(
            f"provider `{provider_id}` 不存在：找不到 {path}。"
            + (f" 可用的 provider：{available}" if available else " 目录为空或不存在")
        )
    return provider_from_dict(load_json(path), f"provider `{provider_id}` ({path.name})")


def load_profile(profile_id: str, profiles_dir: Path) -> TestProfile:
    path = profiles_dir / f"{profile_id}.json"
    if not path.is_file():
        available = sorted(p.stem for p in profiles_dir.glob("*.json")) if profiles_dir.is_dir() else []
        raise ConfigError(
            f"profile `{profile_id}` 不存在：找不到 {path}。"
            + (f" 可用的 profile：{available}" if available else " 目录为空或不存在")
        )
    return profile_from_dict(load_json(path), f"profile `{profile_id}` ({path.name})")


def resolve(provider: ProviderConfig, model: str | None, profile: TestProfile) -> ResolvedConfig:
    effective_model = model or provider.default_model
    if not effective_model:
        raise ConfigError(f"provider `{provider.id}` 未设置 default_model，且未通过 --model 指定模型")
    suites = set(profile.suites)
    protocols = set(provider.protocols)
    if "cache" in suites:
        if profile.cache.mode == "anthropic_explicit" and "anthropic" not in protocols:
            raise ConfigError(
                f"profile 的 cache.mode=anthropic_explicit 要求 provider `{provider.id}` 启用 anthropic 协议"
            )
        if profile.cache.mode == "openai_observation" and "openai" not in protocols:
            raise ConfigError(
                f"profile 的 cache.mode=openai_observation 要求 provider `{provider.id}` 启用 openai 协议"
            )
        if not protocols & {"openai", "anthropic"}:
            raise ConfigError(f"cache suite 要求 provider `{provider.id}` 至少支持 openai 或 anthropic 协议")
    if "reasoning" in suites or "latency" in suites:
        if "openai" not in protocols:
            raise ConfigError(f"reasoning/latency suite 依赖 OpenAI SSE，provider `{provider.id}` 未启用 openai 协议")
    return ResolvedConfig(provider=provider, model=effective_model, profile=profile)
