# llm_bench — LLM API Benchmark Toolkit

配置驱动的 LLM API 基准测试工具包。核心指标优先级：**TTFT** 与 **缓存命中率**。

只使用 Python 标准库（无第三方运行时依赖）。

本目录可以作为独立仓库上传到 GitHub。供应商差异集中在 `providers/*.json`，测试方案集中在 `profiles/*.json`；API Key、生成视频、工具二进制和运行结果均不进入源码仓库。

## 一键完整测试

`Run-FullBenchmark.ps1`依次完成 TTFT、缓存、推理强度与独立思考关闭探针、错误率、视频输入上限矩阵，并生成合并报告：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
$env:SHUYANAI_API_KEY = '<your-key>'
& D:\Dev\test\k3\benchmark-kit\scripts\Run-FullBenchmark.ps1 `
  -Provider shuyanai `
  -Model kimi-k3
Remove-Item Env:SHUYANAI_API_KEY -ErrorAction SilentlyContinue
```

合并报告为常规运行目录中的 `report/complete_benchmark_report.md`，机器可读摘要为 `normalized/complete_summary.json`。这是正式性能测试，会产生多次真实请求和费用；仅测试连通性时不要使用该命令。

只检查最终配置和输出位置，不访问网络、不需要 Key：

```powershell
& D:\Dev\test\k3\benchmark-kit\scripts\Run-FullBenchmark.ps1 `
  -Provider shuyanai `
  -Model kimi-k3 `
  -PlanOnly
```

## 工具定位

- 把「测什么」（provider 配置）和「怎么测」（profile 配置）从代码里分离出来，避免复制大脚本。
- 每次运行输出统一结构的结果（manifest + raw + normalized + report），支持离线重新分析与重新出报告。
- 报告首页即核心性能摘要：TTFT P50/P95、预热后缓存命中率、错误率。
- reasoning 套件报告会直接说明是否支持用 `reasoning_effort=none` 关闭独立思考。
- 视频上限入口会报告 100→50 MiB 受控矩阵中的最大成功档位与相邻失败档位。
- 全链路可离线验证：`dry-run`、单元测试、`reanalyze`、`report` 均不访问网络。

## 目录职责

```text
benchmark-kit/
├─ pyproject.toml          # 包定义（零运行时依赖）
├─ scripts/                # 一键完整测试与视频上限入口
├─ video-limit/            # 真实编码视频矩阵、临时 HTTPS 托管与边界报告
├─ src/llm_bench/
│  ├─ cli.py               # 命令行入口（dry-run/run/reanalyze/report）
│  ├─ config.py            # provider/profile 加载与校验
│  ├─ models.py            # 数据模型（ProviderConfig/TestProfile/NormalizedRequest）
│  ├─ runner.py            # suite 编排（smoke/reasoning/latency/cache/video）
│  ├─ store.py             # 结果持久化 + 敏感信息脱敏
│  ├─ legacy.py            # 历史结果（schema v1/v2/v3）离线重分析适配器
│  ├─ transport/           # http.py（urllib 与可注入 FakeTransport）、sse.py（SSE 解析）
│  ├─ protocols/           # openai.py、anthropic.py（headers/payload/流式语义/usage 归一）
│  ├─ metrics/             # latency.py（TTFT/分位）、cache.py（缓存命中）
│  └─ reporters/           # markdown.py、json_report.py、csv_report.py
├─ providers/              # 供应商配置（不含任何真实 Key）
├─ profiles/               # 测试方案配置
├─ examples/               # 自定义 provider 示例
└─ tests/                  # 离线单元测试 + fixtures（含历史结果样本）
```

## Python 版本

- 要求 Python >= 3.10（开发环境为 3.12）。
- 仅标准库：argparse/json/csv/urllib/threading/unittest 等。

## 安装方式

方式一（推荐，不改环境）——设置 `PYTHONPATH`：

```powershell
$env:PYTHONPATH = 'D:\Dev\test\k3\benchmark-kit\src'
python -m llm_bench --help
```

方式二——可编辑安装（获得 `llm-bench` 命令）：

```powershell
pip install -e D:\Dev\test\k3\benchmark-kit
llm-bench --help
```

## dry-run：查看最终配置，不请求 API

```powershell
$env:PYTHONPATH = 'D:\Dev\test\k3\benchmark-kit\src'
python -m llm_bench dry-run `
  --provider tostco `
  --model kimi-k3 `
  --profile standard
```

输出合并后的 provider、profile、缓存前缀策略等，`network_requests_executed` 恒为 `false`。

## 正式运行（会产生真实 API 调用与费用）

```powershell
$env:TOSTCO_API_KEY = '<你的 API Key>'
python -m llm_bench run `
  --provider tostco `
  --model kimi-k3 `
  --profile standard
Remove-Item Env:TOSTCO_API_KEY
```

- API Key 从 provider 配置的 `api_key_env` 环境变量读取；缺失时报配置错误（退出码 2），不发起任何请求。
- 结果写入 `runs/<provider>/<model>/<timestamp>/`。
- 退出码：`0` 全部成功；`1` 存在失败请求；`2` 参数或配置错误；`3` 内部错误。

## 视频输入上限测试（真实调用，可能计费）

通用入口读取 `providers/` 中的 provider 配置，并复用同一套 60 秒、640×480、25fps、H.264/AAC 真实编码视频。默认依次测试 100、90、80、70、60、50 MiB，同时用 `reasoning_effort=none` 与 `low` 检查独立思考开关。

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
$env:SHUYANAI_API_KEY = '<your-key>'
& D:\Dev\test\k3\benchmark-kit\scripts\Run-VideoLimit.ps1 `
  -Provider shuyanai `
  -Model kimi-k3
Remove-Item Env:SHUYANAI_API_KEY -ErrorAction SilentlyContinue
```

只生成/复用视频并检查临时公网 URL，不调用模型、不产生模型用量：

```powershell
& D:\Dev\test\k3\benchmark-kit\scripts\Run-VideoLimit.ps1 `
  -Provider shuyanai `
  -Model kimi-k3 `
  -PreflightOnly
```

常用开关：

- `-SkipReasoningSwitch`：不执行额外的独立思考开/关探针。
- `-ForceReencode`：忽略已有合格视频，重新编码六个档位；默认复用，节省时间。
- `-ProvidersDir <目录>`：指定自定义 provider 配置目录。

在当前双目录工作区中，结果写入 `../completed-tests/providers/<provider>/video-limit-runs/<timestamp>/`；独立复制本目录时则写入 `runs/video-limit/<provider>/`：

- `benchmark_report.md`：最大成功档位、边界说明、逐档结果、独立思考开关结论。
- `summary.json`：相同结论的机器可读版本。
- `results.json` / `results.csv`：逐视频请求明细。
- `*-response.json` / `*-error.txt`：原始成功响应或错误正文。

这里测试的是 Chat Completions 的 `video_url` 公网 URL 输入限制，不是 multipart 本地文件直传。报告中的“最大成功档位”只是本轮矩阵内的最大成功文件；只有同时存在更大的失败档位，才能形成可信的成功下界和失败上界。

## reanalyze：对已有结果重新分析，不访问网络

```powershell
python -m llm_bench reanalyze `
  --run D:\Dev\test\k3\tostco\benchmark_results\20261014_175604_247854 `
  --output D:\Dev\test\k3\completed-tests\toolkit-runs\reanalyzed
```

- 读取旧目录的 `raw/manifest.json`、`raw/requests/*.json`（或仅 `cleaned/requests.json`）。
- 输出全新的统一布局目录；**不修改、不移动原历史目录**。

### 历史数据的缓存字段语义（默认 unknown）

历史 manifest 从未记录 `input_tokens` 是否包含缓存 Token，协议名本身不是可 relied 的证据，因此 reanalyze **默认按 `unknown` 处理**：Token 缓存命中率为 `N/A`（不输出伪精确百分比），原始缓存字段原样保留，并在报告中输出明确警告。

如需得到精确命中率，必须显式指定语义，优先级：**用户覆盖 > provider 配置 > unknown**：

```powershell
# 方式一：--provider 使用 provider 配置中声明的语义（要求有数据依据）
python -m llm_bench reanalyze --run <历史目录> --provider tostco

# 方式二：--input-token-semantics 显式覆盖（可重复；参数非法时退出码 2）
python -m llm_bench reanalyze --run <历史目录> `
  --input-token-semantics anthropic=includes_cache

# 两者同时给出时，覆盖值优先
```

- 不指定 `--provider` 时，工具仅在 manifest 证据（`provider_name` 或 `base_url`）**唯一匹配**某个 provider 配置时自动采用其声明语义；证据缺失或有歧义（匹配到多个配置）时保持 unknown，绝不猜测。
- 覆盖值直接决定命中率公式（includes_cache 与 excludes_cache 的结果可能相差近一倍），**必须以供应商官方文档或网关实测数据为依据**；使用覆盖时报告会附"命中率公式随覆盖值改变，请确认有数据依据"的警告。
- manifest 记录 `input_token_semantics` / `input_token_semantics_source`（`user_override` / `provider_config` / `unknown`）/ `input_token_semantics_override` / `provider_evidence`，便于审计口径来源。

## report：根据规范化结果重新生成报告，不访问网络

```powershell
python -m llm_bench report `
  --run D:\Dev\test\k3\completed-tests\toolkit-runs\reanalyzed\20261014_175604_247854 `
  --output D:\Dev\test\k3\completed-tests\toolkit-runs\reports\tostco-demo
```

要求 `--run` 目录下存在 `normalized/requests.json`（新运行或 reanalyze 的产物）。

## provider 配置示例

`providers/tostco.json`：

```json
{
  "id": "tostco",
  "display_name": "Tostco",
  "base_url": "https://tostco.ai/v1",
  "default_model": "kimi-k3",
  "protocols": ["openai", "anthropic"],
  "api_key_env": "TOSTCO_API_KEY",
  "input_token_semantics": {
    "anthropic": "unknown",
    "openai": "includes_cache"
  },
  "gateway_cache_fields": ["claude_cache_creation_1h_tokens"]
}
```

字段说明：`input_token_semantics` 声明各协议 `input_tokens` 是否已包含缓存 Token（见下文口径）；**只有拿到供应商官方定义或网关实测证据时才允许声明 `excludes_cache`/`includes_cache`，否则一律写 `unknown`**。`protocol_options` 可覆盖 `chat_path`/`models_path`/`messages_path`/`max_tokens_field`/`include_reasoning_effort`/`send_authorization_header` 等兼容参数。

**禁止在 provider 配置中写入真实 API Key**——只允许写环境变量名。

## profile 配置示例

`profiles/standard.json`：

```json
{
  "suites": ["smoke", "reasoning", "latency", "cache"],
  "timeout_seconds": 180,
  "input_sizes": [1024, 8192],
  "concurrency_levels": [1, 4],
  "samples_per_group": 5,
  "reasoning_samples": 3,
  "max_output_tokens": 64,
  "cache": {
    "mode": "auto",
    "stream": true,
    "cold_start_requests": 1,
    "warm_requests": 4,
    "unique_prefix_per_run": true,
    "varying_suffix": true
  }
}
```

缓存测试支持三种模式：

- `auto`：Anthropic 协议优先（与旧脚本一致），否则用 OpenAI 观测 `cached_tokens`。
- `anthropic_explicit`：在 system 块上显式加 `cache_control: {"type": "ephemeral"}`（要求 anthropic 协议）。
- `openai_observation`：OpenAI 重复前缀请求，观测 `usage.prompt_tokens_details.cached_tokens`。

`unique_prefix_per_run=true` 时每次运行生成唯一前缀（含 provider id + run id），避免复用历史测试缓存。

### 缓存探测的流式与非流式（cache.stream）

- `cache.stream=true`（所有内置 profile 的默认值）：缓存探测以流式发送，**同一请求同时得到 TTFT 与缓存 Token 计数**，报告才能给出真实的"命中/未命中 TTFT 与总耗时对比"。
  - OpenAI 协议自动携带 `stream_options: {"include_usage": true}`，从最后一个 usage 块的 `prompt_tokens_details.cached_tokens` 取值；
  - Anthropic 协议从 `message_start` 的 input usage（含缓存字段）与 `message_delta` 的 output usage 合并；`anthropic_explicit` 模式继续在 system 块上携带 `cache_control`。
- `cache.stream=false`：探测退回非流式，可得到 usage 但**没有 TTFT**——这些请求按 `non_streaming_request` 处理，不算 TTFT 缺失样本，报告中命中/未命中 TTFT 显示 N/A。
- 流式响应**缺少 usage 时**：该请求的缓存指标不可统计（`cache_metrics_valid=false`），不伪造任何数值，并输出"usage 中未发现任何缓存字段"警告；TTFT 仍然有效。

## 添加新供应商

1. 复制 `examples/custom_provider.json` 到 `providers/<新id>.json`，修改 `base_url`、`api_key_env`、协议与缓存语义。
2. 如网关行为特殊，调整 `protocol_options`（路径、max_tokens 字段名、是否发送 Authorization 头等）。
3. `python -m llm_bench dry-run --provider <新id> --profile smoke` 验证配置。
4. 无需修改任何 Python 代码。

## 添加新协议

1. 在 `src/llm_bench/protocols/` 新建模块，实现 `Protocol` 接口：`headers`、`usage_fields`、`input_token_semantics`、`parse_stream`，并调用 `register(<ProtocolClass>())`。
2. 在 `models.py` 的 `SUPPORTED_PROTOCOLS` 加入名字。
3. 在 `runner.py` 增加对应 suite 的调用点（或复用现有 suite）。
4. 为流式语义编写 fixtures 与单元测试（参考 `tests/test_stream_protocols.py`）。

## TTFT 定义

- **TTFT eligible（参与 TTFT 统计的样本）**：`stream=true` 且属于 `reasoning / latency / 流式 cache` 套件的请求。preflight（流式预检）与 smoke/video 套件不参与 TTFT 统计。
- **标准 TTFT**：从请求发出，到收到第一个**非空 reasoning 或 content 语义增量**的耗时（毫秒）。
- 同时保留：`first_byte_ms`（可测量时）、`first_event_ms`（首个 SSE data 事件）、`first_reasoning_ms`、`first_content_ms`。
- `ttft_source` 标注 TTFT 由 reasoning 还是 content 启动；reasoning 增量（思考）与正文增量均可启动 TTFT；**空字符串增量（`text_delta`/`thinking_delta`/`content` 为空串）不启动 TTFT**，以之后第一个非空增量的时间为准。
- HTTP 2xx 但没有任何语义增量时：**不把 total_ms 当作 TTFT**，`ttft_valid=false`，记录 `ttft_missing_reason`（如 `no_semantic_delta`），并在汇总中单独统计缺失样本。
- **非流式请求不是 TTFT 缺失样本**：`ttft_valid=false`、原因 `non_streaming_request`，但不计入缺失统计（它们本来就无法产生 TTFT，属于口径之外）。
- TTFT 缺失只统计 eligible 请求：流式 2xx 无语义增量、流式 HTTP 错误、流式传输错误。

### 双错误率

- **全局错误率（global_error_rate）**：覆盖全部正式请求（所有 suite，**不含 preflight**）的失败比例。
- **TTFT 错误率（ttft_error_rate）**：仅覆盖 TTFT eligible 请求的失败比例。
- 两者在报告核心摘要中分开列出，不混用。

### 核心 TTFT 的来源

核心摘要中的 TTFT P50/P95 **优先取 latency 套件**（可控输入长度与并发）；无 latency 套件时取全部 eligible 请求。manifest 的 `ttft_scope` 字段标注实际来源（`latency` / `eligible_all`）。报告的 TTFT 章节按总体 / latency / reasoning / 流式 cache 套件分别列出。

### 分位数算法

**最近秩法（nearest-rank）**：对 n 个样本取第 p 百分位时，`rank = ceil(p/100 * n)`（下限 1），结果为升序序列的第 rank 个值。样本为空时返回 `N/A`。与历史脚本口径一致。

### TTFT 汇总字段

`attempted / successful / valid_samples / missing_samples / missing_reasons / P50 / P90 / P95 / P99 / min / max / error_rate`，支持按 `provider / model / protocol / input size / concurrency / reasoning effort` 分组（报告内默认按输入长度×并发分组，缺分组键的样本不进入分组表，不会出现 None/None 分组；JSON 中含分组明细）。

## 缓存命中率定义

缓存指标按协议区分字段语义，**不使用一套公式处理所有供应商**：

### 字段语义（input_token_semantics）

- `excludes_cache`（Anthropic）：`usage.input_tokens` 不含缓存 Token。
  - `total_logical_input_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens`
  - `token_cache_hit_rate = cache_read_input_tokens / total_logical_input_tokens`
- `includes_cache`（OpenAI）：`usage.prompt_tokens` 已包含 `prompt_tokens_details.cached_tokens`。
  - `total_logical_input_tokens = input_tokens`
  - `token_cache_hit_rate = cache_read_input_tokens / input_tokens`
- `unknown`：不输出伪精确命中率（命中率为 `N/A`），保留原始字段并输出警告。

### 缓存字段来源

- Anthropic：`usage.cache_read_input_tokens`、`usage.cache_creation_input_tokens`
- OpenAI：`usage.prompt_tokens_details.cached_tokens`
- 网关扩展字段：如 `claude_cache_creation_1h_tokens`，原样保留在 usage 中

### 命中范围（cache_hit_scope）与冷启动基线

不以 `cache_read > 0` 直接判定目标前缀命中，而以**冷启动基线**判定：

- **冷启动基线 `cold_baseline_cache_read_tokens`** = 成功的冷启动请求中最大的 `cache_read`（多个冷启动请求时取最大值，保守口径）。它代表本轮开始前网关/系统提示等公共前缀已有的缓存。
- `none`：无缓存读取（`cache_read == 0`）；
- `any`：冷启动请求上的任意缓存读取，或预热请求的 `cache_read` **未超出**冷启动基线——可能只是网关公共前缀或系统提示缓存，不能归因于本轮唯一前缀；
- `target_prefix`：预热请求的 `cache_read` **明显超出冷启动基线**（增量记入 `target_prefix_cache_read_tokens = max(cache_read - 基线, 0)`，`target_prefix_evidence=true`）；
- `unknown`：非缓存测试，或缺少序号/阶段/基线信息，无法判断。

**预热后请求缓存命中率**的口径是目标前缀：只统计预热请求中 `cache_read` 超出冷启动基线的命中；报告同时展示任意缓存请求命中率与目标前缀增量 Token。工具不使用任何"拍脑袋"阈值——超出基线即算证据，基线本身随每次运行记录在结果中。

### 缓存汇总输出

缓存测试请求数、成功数、可统计数、任意缓存请求命中率、目标前缀请求命中率、冷启动基线 Token、预热后目标前缀请求命中率、Token 加权命中率、预热后 Token 加权命中率、目标前缀增量 Token、缓存创建/读取/未缓存 Token、命中与未命中请求的 TTFT P50/P95 与总耗时 P50/P95、字段来源、字段语义、统计警告、逐请求明细（含阶段/基线/目标前缀增量/TTFT 有效性）。

### 一致性保证

命中率**绝不会被截断到 100%**：当 `cache_read > total_logical_input_tokens` 等不一致出现时，该值被拒绝输出（N/A）并记录校验警告。

## 安全和脱敏说明

- API Key 只通过环境变量传入，绝不写入配置、代码或文档。
- 持久化前统一脱敏：
  - 请求/响应头：`Authorization`、`x-api-key`、`Cookie`、`Set-Cookie`、`Proxy-Authorization` 替换为 `[REDACTED]`；
  - URL 查询参数：名字以 `key`/`token`/`secret`/`signature`/`auth` 结尾或属于 `access_token`/`apikey` 等的参数值替换为 `[REDACTED]`（`max_tokens` 等普通参数不受影响）。
- manifest 记录 `secrets_persisted: false` 作为声明；脱敏函数有单元测试覆盖。
- 输出目录包含模型生成内容，分享前仍应自行检查业务数据。

## 结果目录说明

```text
runs/<provider>/<model>/<timestamp>/
├─ manifest.json              # 工具版本/provider/model/profile/时间/Python/平台/参数/
│                             # 请求数/成功数/失败数/network_requests_executed/secrets_persisted
├─ raw/
│  └─ requests/*.json         # 每个请求的完整记录（已脱敏；流式含逐事件时间戳）
├─ normalized/
│  ├─ requests.json           # 规范化行（含 TTFT 七字段与缓存归一字段）
│  ├─ requests.csv
│  └─ summary.json            # 与 Markdown 报告一致的核心指标（可机读）
└─ report/
   └─ benchmark_report.md     # 首页为核心性能摘要
```

所有记录与汇总均带 `schema_version`（当前为 5）。

## 测试方法

全部离线，不需要任何 API Key：

```powershell
cd D:\Dev\test\k3\benchmark-kit
$env:PYTHONPATH = 'D:\Dev\test\k3\benchmark-kit\src'
python -m unittest discover -s tests -v
```

覆盖：配置校验、OpenAI/Anthropic 流式 SSE 语义（含空增量不启动 TTFT）、TTFT eligible 口径与缺失样本、核心 TTFT 优先 latency、双错误率分离、无 None/None 分组、双协议缓存口径、unknown 语义（新运行与历史 reanalyze）、命中率不超 100%、冷启动基线与 target_prefix 判定、流式缓存同时采集 TTFT 与缓存字段、缺 usage 不可统计、cache.stream=false 非流式回退、脱敏、Markdown/JSON/CSV 输出、历史结果 reanalyze（默认 unknown、显式覆盖、provider 唯一证据自动识别、CLI 参数非法退出码 2、原目录不被修改）、dry-run 与 reanalyze 的无网络断言（patch socket）、兼容入口 dry-run。

## 已知限制

- `run`/`reanalyze`/`report` 的并发模型与旧脚本一致：仅 latency 套件按并发等级并行，其余串行。
- 首字节时间（TTFB）在流式下以首个数据块到达时间为近似值；urllib 不暴露更细的 socket 级时间戳。
- 历史数据的缓存字段语义**默认按 unknown 处理**（历史 manifest 未记录该信息，协议名不作为证据）：Token 命中率为 N/A，须通过 `--provider` 或 `--input-token-semantics` 显式指定后才会计算，报告中以警告明示口径来源。
- 冷启动基线取成功冷启动请求的最大 `cache_read`；若网关在预热阶段才建立公共前缀缓存，个别 `any` 判定偏保守（宁可少算目标前缀命中，不虚报）。
- `video` 套件沿用了固定示例视频 URL，仅作能力探测。
- Windows 控制台以 GBK 显示中文时可能出现乱码，落盘文件均为 UTF-8。

## 旧脚本迁移说明

| 旧入口 | 状态 | 说明 |
|---|---|---|
| `llm_api_benchmark.py` | 兼容入口 | 旧参数继续可用，内部调用统一框架，flat 输出布局，运行时输出弃用提示 |
| `tostco/tostco_kimi_k3_benchmark.py` | 薄适配器 | 保持不变，继续通过兼容入口工作 |
| `tockenStars/tokenstars_kimi_k3_benchmark.py` | 历史脚本 | 未改动；新测试请用 `--provider tokenstars` |
| `july/*` | 历史脚本与结果 | 未改动；其结果可用 `reanalyze` 离线重分析 |

新开发一律使用 `python -m llm_bench`；旧脚本仅在需要复现历史口径时使用。
