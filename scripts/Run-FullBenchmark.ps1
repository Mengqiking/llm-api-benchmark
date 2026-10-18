[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Provider,
    [string]$Model = '',
    [string]$Profile = 'standard',
    [string]$ProvidersDir = (Join-Path $PSScriptRoot '..\providers'),
    [int]$VideoPort = 18765,
    [switch]$PlanOnly,
    [switch]$SkipVideoLimit,
    [switch]$ForceVideoReencode
)

$ErrorActionPreference = 'Stop'
$toolkitRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $toolkitRoot '..')).Path
$completedRoot = Join-Path $workspaceRoot 'completed-tests'
$standalone = -not (Test-Path -LiteralPath $completedRoot)
$benchmarkOutput = if ($standalone) { Join-Path $toolkitRoot 'runs' } else { Join-Path $completedRoot 'toolkit-runs' }

$providerFile = Join-Path $ProvidersDir "$Provider.json"
if (-not (Test-Path -LiteralPath $providerFile)) {
    throw "Provider '$Provider' not found: $providerFile"
}
$providerConfig = Get-Content -LiteralPath $providerFile -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($Model)) { $Model = [string]$providerConfig.default_model }
$plan = [ordered]@{
    network_requests_executed = $false
    provider = $Provider
    model = $Model
    profile = $Profile
    base_url = [string]$providerConfig.base_url
    api_key_env = [string]$providerConfig.api_key_env
    benchmark_output = $benchmarkOutput
    video_limit_enabled = -not $SkipVideoLimit
    complete_report = 'report/complete_benchmark_report.md under the new benchmark run'
}
if ($PlanOnly) {
    $plan | ConvertTo-Json -Depth 10
    return
}
$keyName = [string]$providerConfig.api_key_env
$keyItem = Get-Item -LiteralPath "Env:$keyName" -ErrorAction SilentlyContinue
if (-not $keyItem -or [string]::IsNullOrWhiteSpace($keyItem.Value)) {
    throw "Set environment variable $keyName before running the benchmark."
}

$env:PYTHONPATH = Join-Path $toolkitRoot 'src'
$started = Get-Date
Write-Host "Running full benchmark: provider=$Provider model=$Model profile=$Profile" -ForegroundColor Cyan
& python -m llm_bench run `
    --provider $Provider `
    --model $Model `
    --profile $Profile `
    --providers-dir $ProvidersDir `
    --profiles-dir (Join-Path $toolkitRoot 'profiles') `
    --output $benchmarkOutput
$benchmarkExit = $LASTEXITCODE
if ($benchmarkExit -gt 1) { throw "llm_bench failed with exit code $benchmarkExit." }

$benchmarkParent = Join-Path $benchmarkOutput "$Provider\$Model"
$benchmarkRun = Get-ChildItem -LiteralPath $benchmarkParent -Directory |
    Where-Object { $_.LastWriteTime -ge $started.AddMinutes(-1) } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $benchmarkRun) { throw "Could not locate the new benchmark run under $benchmarkParent." }

$videoRun = $null
if (-not $SkipVideoLimit) {
    $videoOutput = if ($standalone) {
        Join-Path $toolkitRoot "runs\video-limit\$Provider"
    } else {
        Join-Path $completedRoot "providers\$Provider\video-limit-runs"
    }
    $videoStarted = Get-Date
    & (Join-Path $PSScriptRoot 'Run-VideoLimit.ps1') `
        -Provider $Provider `
        -Model $Model `
        -ProvidersDir $ProvidersDir `
        -Port $VideoPort `
        -SkipReasoningSwitch `
        -ForceReencode:$ForceVideoReencode
    $videoRun = Get-ChildItem -LiteralPath $videoOutput -Directory |
        Where-Object { $_.LastWriteTime -ge $videoStarted.AddMinutes(-1) } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $videoRun) { throw "Could not locate the new video-limit run under $videoOutput." }
}

$benchmarkSummaryPath = Join-Path $benchmarkRun.FullName 'normalized\summary.json'
$benchmarkSummary = Get-Content -LiteralPath $benchmarkSummaryPath -Raw | ConvertFrom-Json
$videoSummary = $null
if ($videoRun) {
    $videoSummary = Get-Content -LiteralPath (Join-Path $videoRun.FullName 'summary.json') -Raw | ConvertFrom-Json
}

$complete = [ordered]@{
    schema_version = 1
    generated_at = (Get-Date).ToString('o')
    provider = $Provider
    provider_display_name = [string]$providerConfig.display_name
    model = $Model
    benchmark_run = $benchmarkRun.FullName
    video_limit_run = if ($videoRun) { $videoRun.FullName } else { $null }
    core = $benchmarkSummary.core
    cache = $benchmarkSummary.cache
    reasoning_switch = $benchmarkSummary.reasoning_switch
    video_limit = if ($videoSummary) {
        [ordered]@{
            maximum_successful_video_tier = $videoSummary.maximum_successful_video_tier
            maximum_successful_video_bytes = $videoSummary.maximum_successful_video_bytes
            maximum_successful_video_mib = $videoSummary.maximum_successful_video_mib
            nearest_larger_failed_tier = $videoSummary.nearest_larger_failed_tier
            nearest_larger_failed_bytes = $videoSummary.nearest_larger_failed_bytes
        }
    } else { $null }
}

$completeJson = Join-Path $benchmarkRun.FullName 'normalized\complete_summary.json'
[System.IO.File]::WriteAllText(
    $completeJson,
    ($complete | ConvertTo-Json -Depth 40),
    [System.Text.UTF8Encoding]::new($false)
)

function Format-Metric($value, [string]$suffix = '') {
    if ($null -eq $value) { return 'N/A' }
    return "$value$suffix"
}
function Format-Rate($value) {
    if ($null -eq $value) { return 'N/A' }
    return ('{0:P2}' -f [double]$value)
}

$core = $benchmarkSummary.core
$cachePost = $benchmarkSummary.cache.post_cold
$reasoningStatus = if ($benchmarkSummary.reasoning_switch) { $benchmarkSummary.reasoning_switch.status } else { '未测试' }
$videoTier = if ($videoSummary -and $videoSummary.maximum_successful_video_tier) {
    "$($videoSummary.maximum_successful_video_tier)（$($videoSummary.maximum_successful_video_bytes) 字节）"
} else { '未测试或无成功档位' }
$videoBoundary = if ($videoSummary -and $videoSummary.nearest_larger_failed_tier) {
    "最近更大失败档位：$($videoSummary.nearest_larger_failed_tier)（$($videoSummary.nearest_larger_failed_bytes) 字节）"
} else { '没有形成更大的失败上界' }

$report = @(
    '# LLM 完整基准测试报告', '',
    '## 核心结论', '',
    '|指标|结果|说明|', '|---|---:|---|',
    "|TTFT P50|$(Format-Metric $core.ttft_p50_ms ' ms')|$($core.ttft_scope)|",
    "|TTFT P95|$(Format-Metric $core.ttft_p95_ms ' ms')|有效样本 $($core.ttft_valid_samples)|",
    "|预热后请求缓存命中率|$(Format-Rate $cachePost.request_hit_rate)|目标前缀口径|",
    "|预热后 Token 缓存命中率|$(Format-Rate $cachePost.token_hit_rate)|按 provider 字段语义|",
    "|独立思考开关|$reasoningStatus|reasoning_effort=none 对比正向档位|",
    "|最大成功视频档位|$videoTier|$videoBoundary|",
    "|全局错误率|$(Format-Rate $core.error_rate)|成功 $($core.success)，失败 $($core.failure)|", '',
    '## 基本信息', '',
    "- Provider：``$($providerConfig.display_name)``（``$Provider``）",
    "- Model：``$Model``", "- Profile：``$Profile``",
    "- 常规基准目录：``$($benchmarkRun.FullName)``",
    "- 视频上限目录：``$(if ($videoRun) {$videoRun.FullName} else {'未执行'})``", '',
    '## 报告组成', '',
    "- 常规详细报告：``$(Join-Path $benchmarkRun.FullName 'report\benchmark_report.md')``",
    "- 视频详细报告：``$(if ($videoRun) {Join-Path $videoRun.FullName 'benchmark_report.md'} else {'未执行'})``",
    "- 完整机器可读摘要：``$completeJson``", '',
    '## 口径提醒', '',
    '- 视频测试使用 Chat Completions 的公网 `video_url`，不是 multipart 本地文件直传。',
    '- 最大成功视频档位是本轮测试矩阵中的成功下界；存在更大的失败档位时才形成边界区间。',
    '- 独立思考关闭以 `reasoning_effort=none` 为探针；参数被接受但输出侧没有差异时不会宣称已证实关闭。',
    '- TTFT、缓存和总耗时包含本机网络、供应商网关与模型服务的共同影响。', ''
)
$completeReport = Join-Path $benchmarkRun.FullName 'report\complete_benchmark_report.md'
[System.IO.File]::WriteAllText(
    $completeReport,
    ($report -join [Environment]::NewLine),
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host "Complete report: $completeReport" -ForegroundColor Green
Write-Host "Complete summary: $completeJson" -ForegroundColor Green
