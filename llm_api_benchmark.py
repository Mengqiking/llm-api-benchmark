"""兼容入口（deprecated compatibility entry）。

核心逻辑位于当前目录的 llm_bench 包。本脚本仅保留旧命令行参数，
把参数映射为 provider/profile 后调用统一框架执行。

- 旧参数继续可用：--base-url/--model/--provider-name/--protocols/--suite/
  --quick/--timeout/--reasoning-samples/--samples-per-group/--concurrency/
  --input-sizes/--cache-requests/--output-dir/--dry-run/--api-key
- 输出布局保持旧格式：<output-dir>/<timestamp>/（flat）
- 新开发请直接使用：python -m llm_bench（见 README.md）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_VERSION = "3.2-compat"
ROOT = Path(__file__).resolve().parent
TOOLKIT_SRC = ROOT / "src"

if str(TOOLKIT_SRC) not in sys.path and TOOLKIT_SRC.is_dir():
    sys.path.insert(0, str(TOOLKIT_SRC))

try:
    from llm_bench.config import ConfigError, provider_from_dict, profile_from_dict, resolve
    from llm_bench.models import CacheProfile, TestProfile
    from llm_bench.runner import Runner
    from llm_bench.transport.http import UrllibTransport
except ImportError as exc:  # pragma: no cover
    print(f"无法导入 llm_bench（{exc}）。请确认 {TOOLKIT_SRC} 存在，或先在当前目录执行 `pip install -e .`。",
          file=sys.stderr)
    raise SystemExit(3) from exc


def parse_csv(value: str) -> list[str]:
    return list(dict.fromkeys(x.strip().lower() for x in value.split(",") if x.strip()))


def parse_positive_ints(value: str) -> list[int]:
    try:
        values = [int(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be comma-separated integers") from exc
    if not values or any(x <= 0 for x in values):
        raise argparse.ArgumentTypeError("all values must be positive integers")
    return values


def parse_protocols(value: str) -> list[str]:
    values = parse_csv(value)
    unknown = set(values) - {"openai", "anthropic"}
    if not values or unknown:
        raise argparse.ArgumentTypeError(
            f"protocols must be openai and/or anthropic; invalid: {sorted(unknown)}"
        )
    return values


def parse_suites(value: str) -> list[str]:
    values = parse_csv(value)
    if "full" in values:
        values = ["smoke", "reasoning", "latency", "cache"] + [x for x in values if x != "full"]
    unknown = set(values) - {"smoke", "reasoning", "latency", "cache", "video"}
    if not values or unknown:
        raise argparse.ArgumentTypeError(f"invalid suites: {sorted(unknown)}")
    return list(dict.fromkeys(values))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="[兼容入口] Generic OpenAI/Anthropic-compatible LLM API benchmark (via toolkit)"
    )
    parser.add_argument("--base-url", default=os.environ.get("LLM_API_BASE_URL"),
                        help="API base URL including /v1; or LLM_API_BASE_URL")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL"),
                        help="model identifier; or LLM_MODEL")
    parser.add_argument("--api-key", help="API key; prefer LLM_API_KEY environment variable")
    parser.add_argument("--provider-name", default=os.environ.get("LLM_PROVIDER_NAME", "LLM API"))
    parser.add_argument("--protocols", type=parse_protocols,
                        default=parse_protocols(os.environ.get("LLM_PROTOCOLS", "openai")))
    parser.add_argument("--suite", type=parse_suites, default=parse_suites("smoke"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--reasoning-samples", type=int, default=3)
    parser.add_argument("--samples-per-group", type=int, default=5)
    parser.add_argument("--concurrency", type=parse_positive_ints, default=parse_positive_ints("1,4"))
    parser.add_argument("--input-sizes", type=parse_positive_ints, default=parse_positive_ints("1024,8192"))
    parser.add_argument("--cache-requests", type=int, default=5)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "benchmark_results")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    print(
        f"[deprecated] llm_api_benchmark.py 已成为兼容入口；新开发请使用 "
        f"`python -m llm_bench`（见 README.md）。本次调用将经由统一框架执行。",
        file=sys.stderr,
    )
    if not args.base_url or not args.model:
        parser.error("provide --base-url and --model, or set LLM_API_BASE_URL and LLM_MODEL")
    if min(args.timeout, args.reasoning_samples, args.samples_per_group, args.cache_requests) <= 0:
        parser.error("timeout and sample counts must be positive")
    if "cache" in args.suite and "anthropic" not in args.protocols:
        parser.error("cache suite requires --protocols to include anthropic")
    if args.quick:
        args.reasoning_samples = 1
        args.samples_per_group = 2
        args.concurrency = [1, 4]
        args.input_sizes = [1024]
        args.cache_requests = 3

    provider = provider_from_dict(
        {
            "id": "adhoc",
            "display_name": args.provider_name,
            "base_url": args.base_url.rstrip("/"),
            "default_model": args.model,
            "protocols": args.protocols,
            "api_key_env": "LLM_API_KEY",
        },
        where="compat provider",
    )
    cache_requests = max(args.cache_requests, 1)
    profile = profile_from_dict(
        {
            "suites": args.suite,
            "timeout_seconds": args.timeout,
            "input_sizes": args.input_sizes,
            "concurrency_levels": args.concurrency,
            "samples_per_group": args.samples_per_group,
            "reasoning_samples": args.reasoning_samples,
            "max_output_tokens": 64,
            "cache": {
                "mode": "auto",
                "cold_start_requests": 1,
                "warm_requests": max(cache_requests - 1, 1),
                "unique_prefix_per_run": True,
                "varying_suffix": True,
            },
        },
        where="compat profile",
    )
    try:
        resolved = resolve(provider, args.model, profile)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    config = {
        "script_version": SCRIPT_VERSION,
        "tool_version": "via llm_bench toolkit",
        "provider_name": args.provider_name,
        "base_url": args.base_url.rstrip("/"),
        "model": args.model,
        "protocols": args.protocols,
        "suites": args.suite,
        "timeout": args.timeout,
        "reasoning_samples": args.reasoning_samples,
        "samples_per_group": args.samples_per_group,
        "concurrency": args.concurrency,
        "input_sizes": args.input_sizes,
        "cache_requests": cache_requests,
        "output_dir": str(args.output_dir.resolve()),
        "quick": args.quick,
    }
    if args.dry_run:
        print(
            "[deprecated] dry-run via toolkit；网络请求不会发出。",
            file=sys.stderr,
        )
        import json

        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0

    api_key = args.api_key or os.environ.get("LLM_API_KEY")
    if not api_key:
        parser.error("provide --api-key or set LLM_API_KEY")

    runner = Runner(resolved, UrllibTransport(timeout_seconds=args.timeout))
    summary, _paths = runner.run(args.output_dir.resolve(), api_key, layout="flat")
    failure = (summary.get("core") or {}).get("failure") or 0
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
