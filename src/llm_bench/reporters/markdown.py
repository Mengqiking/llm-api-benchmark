"""Markdown report rendering. The first section after the title is always the
core performance summary (TTFT + cache hit rate), per benchmark policy."""
from __future__ import annotations

from typing import Any

from llm_bench.metrics.latency import percentile_nearest_rank


def _ms(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1f}"


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _pct_labeled(value: float | None, label: str | None) -> str:
    if value is None:
        return "N/A（unknown）" if label == "unknown" else "N/A"
    return f"{value:.2%}"


def render_markdown(summary: dict[str, Any]) -> str:
    manifest = summary.get("manifest") or {}
    core = summary.get("core") or {}
    ttft = summary.get("ttft") or {}
    cache = summary.get("cache")
    reasoning_switch = summary.get("reasoning_switch") or {"status": "未测试"}

    lines: list[str] = ["# Benchmark 报告", ""]

    ttft_scope = core.get("ttft_scope")
    scope_note = (
        "latency 套件" if ttft_scope == "latency" else "全部 TTFT eligible 请求（无 latency 套件）"
    )
    lines += [
        "## 核心性能摘要",
        "",
        "|指标|结果|样本|状态/说明|",
        "|---|---:|---:|---|",
        f"|TTFT P50|{_ms(core.get('ttft_p50_ms'))} ms|{core.get('ttft_valid_samples', 0)}|"
        f"来源：{scope_note}；缺失 {core.get('ttft_missing_samples', 0)} 个|",
        f"|TTFT P95|{_ms(core.get('ttft_p95_ms'))} ms|{core.get('ttft_valid_samples', 0)}|"
        f"来源：{scope_note}；缺失 {core.get('ttft_missing_samples', 0)} 个|",
    ]
    if cache:
        post = cache.get("post_cold") or {}
        cold_seq = (cache.get("cold_start") or {}).get("sequence")
        baseline = cache.get("cold_baseline_cache_read_tokens")
        baseline_note = (
            f"冷启动基线 {baseline} Token；预热后 cache_read 超出基线才算命中"
            if baseline is not None
            else "缺少冷启动基线，无法判定目标前缀命中"
        )
        semantics = cache.get("input_token_semantics") or "unknown"
        lines.append(
            f"|预热后请求缓存命中率|{_pct_labeled(post.get('request_hit_rate'), semantics)}|{post.get('attempted', 0)}|"
            f"口径：目标前缀（冷启动序号 {cold_seq} 之后）；{baseline_note}|"
        )
        increment = post.get("target_prefix_increment_tokens")
        increment_note = f"；目标前缀增量 Token {increment}" if increment is not None else ""
        lines.append(
            f"|预热后 Token 缓存命中率|{_pct_labeled(post.get('token_hit_rate'), semantics)}|{post.get('attempted', 0)}|"
            f"口径：{semantics or 'unknown'}{increment_note}|"
        )
    else:
        lines.append("|预热后请求缓存命中率|N/A|0|本次运行未执行缓存测试|")
        lines.append("|预热后 Token 缓存命中率|N/A|0|本次运行未执行缓存测试|")
    lines.append(
        f"|独立思考开关|{reasoning_switch.get('status', '未测试')}|"
        f"{reasoning_switch.get('off_attempted', 0)} 个关闭探针|口径：reasoning_effort=none 对比正向推理档位|"
    )
    lines.append(f"|全局错误率|{_pct(core.get('error_rate'))}|{core.get('requests', 0)}|成功 {core.get('success', 0)}，失败 {core.get('failure', 0)}（不含 preflight）|")
    lines.append(f"|TTFT 错误率|{_pct(core.get('ttft_error_rate'))}|仅 TTFT eligible 请求|全局错误率与 TTFT 错误率分开统计|")
    lines.append("")

    lines += [
        "## 基本信息",
        "",
        f"- Provider：`{manifest.get('provider')}`",
        f"- Model：`{manifest.get('model')}`",
        f"- Base URL：`{manifest.get('base_url', 'N/A')}`",
        f"- Profile：`{manifest.get('profile')}`",
        f"- 协议：`{','.join(manifest.get('protocols') or [])}`",
        f"- 工具版本：`{manifest.get('tool_version')}`",
        f"- 时间：{manifest.get('started_at')} ~ {manifest.get('completed_at')}",
        f"- 是否执行过网络请求：`{manifest.get('network_requests_executed')}`",
        "",
    ]

    # ---- TTFT ----
    lines += [
        "## TTFT",
        "",
        "### 总体指标",
        "",
        "|attempted|successful|有效样本|缺失样本|P50(ms)|P90(ms)|P95(ms)|P99(ms)|min|max|错误率|",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"|{ttft.get('attempted', 0)}|{ttft.get('successful', 0)}|{ttft.get('valid_samples', 0)}|"
            f"{ttft.get('missing_samples', 0)}|{_ms(ttft.get('ttft_p50_ms'))}|{_ms(ttft.get('ttft_p90_ms'))}|"
            f"{_ms(ttft.get('ttft_p95_ms'))}|{_ms(ttft.get('ttft_p99_ms'))}|{_ms(ttft.get('ttft_min_ms'))}|"
            f"{_ms(ttft.get('ttft_max_ms'))}|{_pct(ttft.get('error_rate'))}|"
        ),
        "",
    ]
    groups = ttft.get("groups") or []
    if groups:
        lines += [
            "### 按输入长度与并发分组",
            "",
            "|输入字符|并发|成功|有效 TTFT|P50(ms)|P90(ms)|P95(ms)|P99(ms)|",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for group in groups:
            g = group.get("group") or {}
            lines.append(
                f"|{g.get('input_size_chars')}|{g.get('concurrency')}|{group.get('successful', 0)}|"
                f"{group.get('valid_samples', 0)}|{_ms(group.get('ttft_p50_ms'))}|{_ms(group.get('ttft_p90_ms'))}|"
                f"{_ms(group.get('ttft_p95_ms'))}|{_ms(group.get('ttft_p99_ms'))}|"
            )
        lines.append("")
    by_suite = ttft.get("by_suite") or {}
    suite_titles = {
        "latency": "### latency 套件 TTFT",
        "reasoning": "### reasoning 套件 TTFT",
        "cache": "### 流式 cache 套件 TTFT",
    }
    for suite in ("latency", "reasoning", "cache"):
        subset = by_suite.get(suite)
        if not subset:
            continue
        lines += [
            suite_titles[suite],
            "",
            "|attempted|successful|有效样本|缺失样本|P50(ms)|P95(ms)|错误率|",
            "|---:|---:|---:|---:|---:|---:|---:|",
            (
                f"|{subset.get('attempted', 0)}|{subset.get('successful', 0)}|{subset.get('valid_samples', 0)}|"
                f"{subset.get('missing_samples', 0)}|{_ms(subset.get('ttft_p50_ms'))}|"
                f"{_ms(subset.get('ttft_p95_ms'))}|{_pct(subset.get('error_rate'))}|"
            ),
            "",
        ]
    reasoning = summary.get("reasoning") or []
    if reasoning:
        lines += [
            "### 按推理强度分组",
            "",
            "|effort|成功|TTFT P50(ms)|TTFT P95(ms)|总耗时 P50(ms)|思考 Token 中位数|思考出现率|",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for item in reasoning:
            lines.append(
                f"|{item['effort']}|{item['successful']}|{_ms(item['ttft_p50_ms'])}|{_ms(item['ttft_p95_ms'])}|"
                f"{_ms(item['total_p50_ms'])}|{_ms(item['thinking_tokens_median'])}|{_pct(item['reasoning_present_rate'])}|"
            )
        lines.append("")
    missing_reasons = ttft.get("missing_reasons") or {}
    if missing_reasons:
        lines += ["### 缺失样本", ""]
        lines += [f"- `{reason}`：{count} 个" for reason, count in sorted(missing_reasons.items()) if count]
        lines.append("")
    lines += [
        "### 口径说明",
        "",
        "- TTFT：从请求发出到收到第一个非空 reasoning 或 content 语义增量的耗时。",
        "- TTFT eligible 样本：stream=true 且属于 reasoning/latency/流式 cache 套件的请求；preflight 与 smoke/video 不参与 TTFT 统计。",
        "- 非流式请求不是 TTFT 缺失样本，不计入缺失统计。",
        "- TTFT 缺失只统计 eligible 请求：HTTP 2xx 但没有非空语义增量、流式 HTTP 错误、流式传输错误。",
        "- 全局错误率覆盖全部正式请求（不含 preflight）；TTFT 错误率只覆盖 eligible 请求，两者分开统计。",
        "- 核心摘要的 TTFT 优先取 latency 套件（可控输入长度与并发）；无 latency 套件时取全部 eligible 请求。",
        "- 同时记录首个 SSE 事件、首个 reasoning 增量、首个正文 content 增量三个时间点。",
        "- HTTP 2xx 但没有任何语义增量时，不把 total_ms 当作 TTFT：ttft_valid=false 并记录缺失原因。",
        "- 分位数采用最近秩法（nearest-rank）：rank = ceil(p/100*n)，取升序第 rank 个样本。",
        "- reasoning 增量（思考）与 content 增量（正文）均可启动 TTFT，ttft_source 标注实际来源；空字符串增量不启动 TTFT。",
        "",
    ]

    # ---- cache ----
    if cache:
        post = cache.get("post_cold") or {}
        cold = cache.get("cold_start") or {}
        baseline = cache.get("cold_baseline_cache_read_tokens")
        semantics = cache.get("input_token_semantics") or "unknown"
        source = cache.get("cache_metric_source") or "unknown"
        formula = {
            "excludes_cache": "cache_read / (input_tokens + cache_creation + cache_read)",
            "includes_cache": "cache_read / input_tokens",
            "unknown": "语义未知，不输出伪精确命中率",
        }.get(semantics, "unknown")
        baseline_text = str(baseline) if baseline is not None else "未知（无法判定目标前缀命中）"
        lines += [
            "## 缓存命中",
            "",
            f"- 缓存请求：{cache.get('successful', 0)}/{cache.get('attempted', 0)} 成功，可统计 {cache.get('countable', 0)}。",
            f"- 冷启动：首个成功请求序号 {cold.get('sequence')}（scope 记为 any，可能只是网关/系统提示缓存）。",
            f"- 冷启动基线 cache_read：{baseline_text} Token（多个冷启动请求时取其中的最大值）。",
            f"- 任意缓存请求命中率：{_pct(cache.get('request_hit_rate'))}（{cache.get('request_hits', 0)} 个命中）。",
            f"- 目标前缀请求命中率：{_pct(cache.get('target_prefix_request_hit_rate'))}"
            f"（{cache.get('target_prefix_request_hits', 0)}/{cache.get('successful', 0)}）。",
            f"- 预热后目标前缀请求命中率：{_pct_labeled(post.get('request_hit_rate'), semantics)}"
            f"（{post.get('request_hits', 0)}/{post.get('attempted', 0)}；仅统计 cache_read 超出冷启动基线的预热请求）。",
            f"- Token 加权命中率：{_pct_labeled(cache.get('token_hit_rate'), semantics)}。",
            f"- 预热后 Token 加权命中率：{_pct_labeled(post.get('token_hit_rate'), semantics)}；"
            f"目标前缀增量 Token：{post.get('target_prefix_increment_tokens')}（全部请求合计 "
            f"{cache.get('target_prefix_cache_read_tokens')}）。",
            f"- 缓存创建 Token：{cache.get('cache_creation_input_tokens', 0)}；缓存读取 Token：{cache.get('cache_read_input_tokens', 0)}；"
            f"未缓存 Token：{cache.get('uncached_input_tokens', 0)}；总逻辑输入 Token：{cache.get('total_logical_input_tokens', 0)}。",
            f"- 字段来源：`{source}`；字段语义：`{semantics}`。",
            f"- 计算公式：`{formula}`。",
            "",
            "### 命中与未命中对比",
            "",
            "|分组|TTFT P50(ms)|TTFT P95(ms)|总耗时 P50(ms)|总耗时 P95(ms)|",
            "|---|---:|---:|---:|---:|",
        ]
        hit = cache.get("hit_ttft") or {}
        miss = cache.get("miss_ttft") or {}
        lines.append(
            f"|缓存命中|{_ms(hit.get('ttft_p50_ms'))}|{_ms(hit.get('ttft_p95_ms'))}|"
            f"{_ms(hit.get('total_p50_ms'))}|{_ms(hit.get('total_p95_ms'))}|"
        )
        lines.append(
            f"|未命中|{_ms(miss.get('ttft_p50_ms'))}|{_ms(miss.get('ttft_p95_ms'))}|"
            f"{_ms(miss.get('total_p50_ms'))}|{_ms(miss.get('total_p95_ms'))}|"
        )
        lines.append("")
        if cache.get("warnings"):
            lines += ["### 缓存统计警告", ""]
            lines += [f"- {w}" for w in cache["warnings"]]
            lines.append("")
        requests = cache.get("requests") or []
        if requests:
            lines += [
                "### 逐请求明细",
                "",
                "|序号|阶段|成功|命中|命中范围|输入 Token(raw)|缓存创建|缓存读取|冷启动基线|目标前缀增量|总逻辑输入|TTFT(ms)|总耗时(ms)|",
                "|---:|---|:---:|:---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
            for item in requests:
                phase = item.get("phase") or "-"
                baseline_cell = item.get("cold_baseline_cache_read_tokens")
                baseline_cell = "-" if baseline_cell is None else baseline_cell
                evidence_cell = item.get("target_prefix_evidence")
                evidence_text = {True: "是", False: "否", None: "-"}[evidence_cell]
                lines.append(
                    f"|{item.get('sequence')}|{phase}|{'是' if item.get('success') else '否'}|"
                    f"{'是' if item.get('cache_hit') else '否'}|{item.get('cache_hit_scope')}|"
                    f"{item.get('input_tokens_raw')}|{item.get('cache_creation_input_tokens')}|"
                    f"{item.get('cache_read_input_tokens')}|{baseline_cell}|{evidence_text}|"
                    f"{item.get('total_logical_input_tokens')}|"
                    f"{_ms(item.get('ttft_ms'))}|{_ms(item.get('total_ms'))}|"
                )
            lines.append("")

    # ---- suite status ----
    lines += ["## 测试组状态", ""]
    suite_status = summary.get("suite_status") or {}
    if suite_status:
        lines += [f"- `{name}`：{status}" for name, status in suite_status.items()]
    else:
        lines.append("- 无请求记录。")
    lines.append("")

    # ---- failures ----
    lines += ["## 异常汇总", ""]
    failures = summary.get("failures") or []
    if failures:
        seen: dict[tuple[Any, Any, Any], int] = {}
        for f in failures:
            key = (f.get("test_name"), f.get("http_status"), f.get("error"))
            seen[key] = seen.get(key, 0) + 1
        for (name, status, error), count in sorted(seen.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {count} 次 `{name}`，HTTP `{status}`：{error}")
    else:
        lines.append("本次测试没有失败请求。")
    lines.append("")

    # ---- caveats ----
    lines += [
        "## 数据口径与限制",
        "",
        "- 成功定义为 HTTP 2xx 且客户端未记录异常。",
        "- 分位数采用最近秩法；只统计成功且存在对应时间字段的样本。",
        "- 缓存命中率绝不会被截断到 100%：输入不一致时输出 N/A 并给出警告。",
        "- 目标前缀命中以冷启动基线判定：预热请求的 cache_read 必须超出冷启动请求中的最大 cache_read，"
        "否则只记为 any（可能来自网关公共前缀或系统提示缓存）。",
        "- 原始请求与响应保存于 raw 目录；Authorization/x-api-key/Cookie 等认证字段与 URL 中的 key/token 参数会被脱敏。",
        "- 测量值包含本机、网络、网关和模型服务的共同影响，不等同于模型节点裸性能。",
    ]
    if summary.get("warnings"):
        lines += ["", "### 汇总警告", ""]
        lines += [f"- {w}" for w in summary["warnings"]]
    lines.append("")
    return "\n".join(lines)
