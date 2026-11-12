"""Command line interface.

Exit codes:
- 0 success
- 1 run executed but some requests failed
- 2 argument/config error
- 3 internal error
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from llm_bench import TOOL_VERSION
from llm_bench.config import (
    ConfigError,
    load_profile,
    load_provider,
    resolve,
)
from llm_bench.legacy import (
    LegacyRunError,
    load_legacy_manifest,
    match_provider_from_manifest,
    reanalyze_run,
)
from llm_bench.models import INPUT_TOKEN_SEMANTICS, SUPPORTED_PROTOCOLS, BenchError
from llm_bench.runner import Runner
from llm_bench.transport.http import UrllibTransport

TOOLKIT_ROOT = Path(__file__).resolve().parents[2]
K3_ROOT = TOOLKIT_ROOT.parent
DEFAULT_PROVIDERS_DIR = TOOLKIT_ROOT / "providers"
DEFAULT_PROFILES_DIR = TOOLKIT_ROOT / "profiles"
_COMPLETED_TESTS_ROOT = K3_ROOT / "completed-tests"
DEFAULT_OUTPUT_ROOT = (
    _COMPLETED_TESTS_ROOT / "toolkit-runs"
    if _COMPLETED_TESTS_ROOT.is_dir()
    else TOOLKIT_ROOT / "runs"
)

EXIT_OK = 0
EXIT_REQUEST_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_INTERNAL_ERROR = 3


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", required=True, help="provider id, e.g. tostco")
    parser.add_argument("--model", default=None, help="model id; defaults to provider default_model")
    parser.add_argument("--profile", required=True, help="profile id, e.g. standard")
    parser.add_argument("--providers-dir", type=Path, default=DEFAULT_PROVIDERS_DIR)
    parser.add_argument("--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR)


def _load_resolved(args: argparse.Namespace):
    provider = load_provider(args.provider, args.providers_dir)
    profile = load_profile(args.profile, args.profiles_dir)
    return resolve(provider, args.model, profile)


def _print_dry_run(resolved: Any, profile_id: str) -> None:
    payload = {
        "command": "dry-run",
        "network_requests_executed": False,
        "provider": {
            "id": resolved.provider.id,
            "display_name": resolved.provider.display_name,
            "base_url": resolved.provider.base_url,
            "protocols": resolved.provider.protocols,
            "api_key_env": resolved.provider.api_key_env,
            "input_token_semantics": resolved.provider.input_token_semantics,
            "cache_metric_source": resolved.provider.cache_metric_source,
        },
        "model": resolved.model,
        "profile_id": profile_id,
        "profile": resolved.profile.to_dict(),
        "cache_prefix_strategy": (
            "每次运行生成唯一前缀（provider + run_id + 随机数），避免复用历史测试缓存"
            if resolved.profile.cache.unique_prefix_per_run
            else "固定共享前缀（unique_prefix_per_run=false）"
        ),
        "tool_version": TOOL_VERSION,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_dry_run(args: argparse.Namespace) -> int:
    resolved = _load_resolved(args)
    _print_dry_run(resolved, args.profile)
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    from llm_bench.demo import run_demo

    print(json.dumps(run_demo(args.output), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    resolved = _load_resolved(args)
    api_key = args.api_key
    if not api_key:
        import os

        api_key = os.environ.get(resolved.provider.api_key_env)
    if not api_key:
        raise ConfigError(
            f"未找到 API Key：请设置环境变量 {resolved.provider.api_key_env}，或使用 --api-key 传入"
        )
    transport = UrllibTransport(timeout_seconds=resolved.profile.timeout_seconds)
    runner = Runner(resolved, transport)
    output_root = args.output.resolve()
    summary, _paths = runner.run(output_root, api_key, layout=args.layout)
    core = summary.get("core") or {}
    if core.get("failure"):
        print(f"完成，但有 {core['failure']} 个失败请求。", flush=True)
        return EXIT_REQUEST_FAILURE
    return EXIT_OK


def parse_semantics_override(value: str) -> tuple[str, str]:
    """Parse <protocol>=<semantics>, e.g. anthropic=includes_cache."""
    protocol, separator, semantics = value.partition("=")
    protocol = protocol.strip().lower()
    semantics = semantics.strip().lower()
    if not separator or protocol not in SUPPORTED_PROTOCOLS or semantics not in INPUT_TOKEN_SEMANTICS:
        raise argparse.ArgumentTypeError(
            f"应为 <protocol>=<semantics>（协议 {list(SUPPORTED_PROTOCOLS)}，"
            f"语义 {list(INPUT_TOKEN_SEMANTICS)}），实际为 {value!r}"
        )
    return protocol, semantics


def cmd_reanalyze(args: argparse.Namespace) -> int:
    run_dir = args.run.expanduser().resolve()
    output_dir = (
        args.output.expanduser().resolve()
        if args.output
        else DEFAULT_OUTPUT_ROOT / "reanalyzed"
    )
    provider = None
    provider_evidence = ""
    if args.provider:
        provider = load_provider(args.provider, args.providers_dir)
    else:
        # Auto-detection uses unique evidence only (provider_name/base_url in
        # the legacy manifest matching exactly one provider config).
        legacy_manifest = load_legacy_manifest(run_dir) or {}
        provider, provider_evidence = match_provider_from_manifest(legacy_manifest, args.providers_dir)
    semantics_override = dict(args.input_token_semantics or [])
    summary, out_root = reanalyze_run(
        run_dir,
        output_dir,
        provider=provider,
        provider_evidence=provider_evidence,
        semantics_override=semantics_override,
    )
    core = summary.get("core") or {}
    print(
        json.dumps(
            {
                "command": "reanalyze",
                "network_requests_executed": False,
                "source": str(run_dir),
                "output": str(out_root),
                "provider": provider.id if provider else (args.provider or None),
                "provider_evidence": provider_evidence or None,
                "input_token_semantics": summary.get("manifest", {}).get("input_token_semantics"),
                "input_token_semantics_source": summary.get("manifest", {}).get("input_token_semantics_source"),
                "requests": core.get("requests"),
                "success": core.get("success"),
                "failure": core.get("failure"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    from llm_bench.reporters import build_summary, render_markdown

    run_dir = args.run.expanduser().resolve()
    normalized = run_dir / "normalized" / "requests.json"
    if not normalized.is_file():
        raise ConfigError(
            f"{normalized} 不存在。report 命令需要规范化结果；请先对原始结果执行 reanalyze。"
        )
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        manifest_path = run_dir / "raw" / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = json.loads(normalized.read_text(encoding="utf-8"))
    summary = build_summary(rows, manifest)
    output_dir = (
        args.output.expanduser().resolve() if args.output else DEFAULT_OUTPUT_ROOT / "reports" / run_dir.name
    )
    (output_dir / "report").mkdir(parents=True, exist_ok=True)
    (output_dir / "report" / "benchmark_report.md").write_text(render_markdown(summary), encoding="utf-8")
    (output_dir / "normalized").mkdir(parents=True, exist_ok=True)
    (output_dir / "normalized" / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "command": "report",
                "network_requests_executed": False,
                "source": str(run_dir),
                "output": str(output_dir / "report" / "benchmark_report.md"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm_bench",
        description="Config-driven LLM API benchmark toolkit (TTFT + cache hit first)",
    )
    parser.add_argument("--version", action="version", version=f"llm_bench {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="用合成响应生成示例报告，不访问网络、不需要 Key")
    demo.add_argument("--output", type=Path, default=Path("runs") / "demo",
                      help="产物根目录；每次创建新的子目录（默认 ./runs/demo）")
    demo.set_defaults(func=cmd_demo)

    dry = sub.add_parser("dry-run", help="解析并显示最终配置，不访问网络")
    _add_config_args(dry)
    dry.set_defaults(func=cmd_dry_run)

    run = sub.add_parser("run", help="执行基准测试（会产生真实 API 请求）")
    _add_config_args(run)
    run.add_argument("--api-key", default=None, help="优先使用 provider 配置的 api_key_env 环境变量")
    run.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT, help="运行产物根目录（默认 k3/runs）")
    run.add_argument("--layout", choices=["nested", "flat"], default="nested",
                     help="nested=runs/<provider>/<model>/<ts>；flat=<output>/<ts>（兼容旧脚本）")
    run.set_defaults(func=cmd_run)

    reana = sub.add_parser("reanalyze", help="对已有 raw 结果重新分析，不访问网络")
    reana.add_argument("--run", type=Path, required=True, help="历史结果目录")
    reana.add_argument("--output", type=Path, default=None,
                       help=f"输出根目录（默认 {DEFAULT_OUTPUT_ROOT / 'reanalyzed'}）")
    reana.add_argument("--provider", default=None,
                       help="provider id：用其声明的 input_token_semantics 补充原 manifest 未记录的缓存语义；"
                            "缺省时仅在 manifest 证据（provider_name/base_url）唯一匹配某个 provider 配置时自动采用")
    reana.add_argument("--providers-dir", type=Path, default=DEFAULT_PROVIDERS_DIR)
    reana.add_argument("--input-token-semantics", type=parse_semantics_override, action="append",
                       metavar="<protocol>=<semantics>",
                       help="显式覆盖某协议的缓存字段语义（如 anthropic=includes_cache），"
                            "可重复；优先级高于 provider 配置。覆盖值直接影响命中率公式，须有数据依据")
    reana.set_defaults(func=cmd_reanalyze)

    report = sub.add_parser("report", help="根据规范化结果重新生成报告，不访问网络")
    report.add_argument("--run", type=Path, required=True, help="包含 normalized/requests.json 的结果目录")
    report.add_argument("--output", type=Path, default=None,
                        help=f"输出目录（默认 {DEFAULT_OUTPUT_ROOT / 'reports' / '<run名>'}）")
    report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse-level argument errors already exit 2
        code = exc.code
        return code if isinstance(code, int) else EXIT_CONFIG_ERROR
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except LegacyRunError as exc:
        print(f"历史结果错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except BenchError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    except Exception:  # pragma: no cover - defensive
        import traceback

        traceback.print_exc()
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
