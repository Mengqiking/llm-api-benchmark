[CmdletBinding()]
param(
    [string]$ApiKey = $env:ONELINK_API_KEY,
    [string]$BaseUrl = 'https://api.onelinkai.cloud',
    [string]$Model = 'kimi-k3',
    [string]$ProviderName = '',
    [string]$Manifest = (Join-Path $PSScriptRoot 'video-cases.json'),
    [ValidateSet('controlled', 'psu', 'all')]
    [string]$Set = 'controlled',
    [ValidateSet('descending', 'ascending')]
    [string]$Order = 'descending',
    [switch]$Execute,
    [switch]$ContinueAfterSuccess,
    [switch]$ProbeReasoningSwitch,
    [int]$TimeoutSec = 1800,
    [string]$OutputRoot = (Join-Path $PSScriptRoot 'runs')
)

$ErrorActionPreference = 'Stop'

function Get-HeaderValue {
    param($Headers, [string]$Name)
    return [string](@($Headers[$Name])[0])
}

function Get-ErrorClassification {
    param([string]$Message)

    if ($Message -match 'Media could not be decoded within supported limits') {
        return 'REMOTE_MEDIA_LIMIT'
    }
    if ($Message -match '401|Unauthorized|InvalidApiKey') {
        return 'REMOTE_AUTH'
    }
    if ($Message -match '429|rate.?limit') {
        return 'REMOTE_RATE_LIMIT'
    }
    return 'REMOTE_OR_TRANSPORT_ERROR'
}

function Get-RemoteErrorText {
    param($ErrorRecord)
    $text = $ErrorRecord.Exception.Message
    if ($ErrorRecord.ErrorDetails -and $ErrorRecord.ErrorDetails.Message) {
        $text = $ErrorRecord.ErrorDetails.Message
    }
    return $text
}

if (-not (Test-Path -LiteralPath $Manifest)) {
    throw "Manifest not found: $Manifest"
}

# Windows PowerShell 5.1 emits a top-level JSON array as one non-enumerated
# pipeline object. Assign first, then explicitly enumerate it so each case is
# processed separately on both Windows PowerShell 5.1 and PowerShell 7+.
$parsedCases = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$cases = @($parsedCases | ForEach-Object { $_ })
if ($Set -ne 'all') {
    $cases = @($cases | Where-Object { $_.set -eq $Set })
}

if ($Order -eq 'descending') {
    $cases = @($cases | Sort-Object expectedBytes -Descending)
}
else {
    $cases = @($cases | Sort-Object expectedBytes)
}

if ($cases.Count -eq 0) {
    throw "No test cases selected for set '$Set'."
}

if ($Execute -and [string]::IsNullOrWhiteSpace($ApiKey)) {
    throw 'A key is required for -Execute. Set ONELINK_API_KEY or pass -ApiKey.'
}

$runId = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$runDir = Join-Path $OutputRoot $runId
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

$results = @()
$reasoningResults = @()
$endpoint = $BaseUrl.TrimEnd('/') + '/v1/chat/completions'
$providerLabel = if ([string]::IsNullOrWhiteSpace($ProviderName)) {
    ([uri]$BaseUrl).Host
} else {
    $ProviderName
}

try {
    foreach ($caseItem in $cases) {
        $displayName = [string]$caseItem.name
        if ($displayName -match '(\d+(?:\.\d+)?)MiB') {
            $displayName = "$($Matches[1])MB"
        }

        Write-Host ''
        Write-Host "========== 测试 $displayName ==========" -ForegroundColor Cyan
        Write-Host '检查远程文件……'

        $actualBytes = $null
        $actualMiB = $null
        $contentType = $null

        try {
            $head = Invoke-WebRequest `
                -Uri $caseItem.url `
                -Method Head `
                -UseBasicParsing `
                -TimeoutSec 120 `
                -ErrorAction Stop

            $actualBytes = [int64](Get-HeaderValue $head.Headers 'Content-Length')
            $contentType = Get-HeaderValue $head.Headers 'Content-Type'
            $actualMiB = [math]::Round($actualBytes / 1MB, 3)

            if ($head.StatusCode -ne 200) {
                throw "HEAD status was $($head.StatusCode), expected 200."
            }
            if ($contentType -notmatch '^video/mp4') {
                throw "Content-Type was '$contentType', expected video/mp4."
            }
            if ([int64]$caseItem.expectedBytes -ne $actualBytes) {
                throw "Content-Length changed: expected $($caseItem.expectedBytes), actual $actualBytes."
            }

            if (-not $Execute) {
                Write-Host "远程文件正常：$actualBytes 字节，$contentType"
            }
        }
        catch {
            $results += [pscustomobject]@{
                Name = $caseItem.name; Set = $caseItem.set; Bytes = $actualBytes
                SizeMiB = $actualMiB; Success = $false; Stage = 'PREFLIGHT'
                Classification = 'LOCAL_OR_SOURCE_ERROR'; ElapsedSeconds = 0
                RequestId = $null; ImageTokens = $null; FinishReason = $null
                Error = $_.Exception.Message; Url = $caseItem.url
            }

            Write-Host "远程文件检查失败：$($_.Exception.Message)" -ForegroundColor Red
            continue
        }

        if (-not $Execute) {
            $results += [pscustomobject]@{
                Name = $caseItem.name; Set = $caseItem.set; Bytes = $actualBytes
                SizeMiB = $actualMiB; Success = $null; Stage = 'PREFLIGHT_ONLY'
                Classification = 'NO_MODEL_CALL'; ElapsedSeconds = 0
                RequestId = $null; ImageTokens = $null; FinishReason = $null
                Error = $null; Url = $caseItem.url
            }

            continue
        }

        $bodyObject = @{
            model = $Model
            messages = @(
                @{
                    role = 'user'
                    content = @(
                        @{ type = 'text'; text = 'Check whether this video can be read. Reply only OK.' },
                        @{ type = 'video_url'; video_url = @{ url = $caseItem.url } }
                    )
                }
            )
            reasoning_effort = 'low'
            max_completion_tokens = 64
            temperature = 1
            stream = $false
        }

        $bodyJson = $bodyObject | ConvertTo-Json -Depth 20
        $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($bodyJson)
        $headers = @{ Authorization = "Bearer $ApiKey"; Accept = 'application/json' }
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $success = $false

        Write-Host "开始模型调用：$actualBytes 字节"

        try {
            $response = Invoke-RestMethod `
                -Uri $endpoint `
                -Method Post `
                -Headers $headers `
                -ContentType 'application/json; charset=utf-8' `
                -Body $bodyBytes `
                -TimeoutSec $TimeoutSec `
                -ErrorAction Stop

            $timer.Stop()
            $success = $true
            $requestId = if ($response.request_id) { $response.request_id } else { $response.id }
            $imageTokens = $response.usage.prompt_tokens_details.image_tokens
            $finishReason = $response.choices[0].finish_reason

            $results += [pscustomobject]@{
                Name = $caseItem.name; Set = $caseItem.set; Bytes = $actualBytes
                SizeMiB = $actualMiB; Success = $true; Stage = 'MODEL'
                Classification = 'SUCCESS'
                ElapsedSeconds = [math]::Round($timer.Elapsed.TotalSeconds, 3)
                RequestId = $requestId; ImageTokens = $imageTokens
                FinishReason = $finishReason; Error = $null; Url = $caseItem.url
            }

            $safeName = ([string]$caseItem.name) -replace '[^A-Za-z0-9._-]', '_'
            [System.IO.File]::WriteAllText(
                (Join-Path $runDir "$safeName-response.json"),
                ($response | ConvertTo-Json -Depth 30),
                [System.Text.UTF8Encoding]::new($false)
            )

            Write-Host "$displayName 成功" -ForegroundColor Green
            Write-Host "耗时：$([math]::Round($timer.Elapsed.TotalSeconds, 3)) 秒"
            Write-Host "image_tokens：$imageTokens"
            Write-Host "request_id：$requestId"
        }
        catch {
            $timer.Stop()
            $errorText = Get-RemoteErrorText $_

            $requestId = $null
            if ($errorText -match '"req_id"\s*:\s*"([^"]+)"') {
                $requestId = $Matches[1]
            }
            $classification = Get-ErrorClassification $errorText

            $results += [pscustomobject]@{
                Name = $caseItem.name; Set = $caseItem.set; Bytes = $actualBytes
                SizeMiB = $actualMiB; Success = $false; Stage = 'MODEL'
                Classification = $classification
                ElapsedSeconds = [math]::Round($timer.Elapsed.TotalSeconds, 3)
                RequestId = $requestId; ImageTokens = $null; FinishReason = $null
                Error = $errorText; Url = $caseItem.url
            }

            $safeName = ([string]$caseItem.name) -replace '[^A-Za-z0-9._-]', '_'
            [System.IO.File]::WriteAllText(
                (Join-Path $runDir "$safeName-error.txt"),
                $errorText,
                [System.Text.UTF8Encoding]::new($false)
            )

            Write-Host "$displayName 失败" -ForegroundColor Red
            Write-Host "耗时：$([math]::Round($timer.Elapsed.TotalSeconds, 3)) 秒"
            Write-Host "request_id：$requestId"
            Write-Host "错误：$errorText"
        }

        if ($success -and -not $ContinueAfterSuccess) {
            Write-Host '已找到第一次成功，停止测试更小文件。' -ForegroundColor Yellow
            break
        }
    }

    if ($Execute -and $ProbeReasoningSwitch) {
        Write-Host ''
        Write-Host '========== 独立思考开关探针 ==========' -ForegroundColor Cyan
        foreach ($probe in @(
            @{ Name = 'off'; Effort = 'none' },
            @{ Name = 'on'; Effort = 'low' }
        )) {
            $reasoningBody = @{
                model = $Model
                messages = @(@{ role = 'user'; content = '计算 37×41，只回复最终整数。' })
                reasoning_effort = $probe.Effort
                max_completion_tokens = 64
                temperature = 1
                stream = $false
            }
            $reasoningBytes = [System.Text.Encoding]::UTF8.GetBytes(
                ($reasoningBody | ConvertTo-Json -Depth 10)
            )
            $reasoningTimer = [System.Diagnostics.Stopwatch]::StartNew()
            try {
                $reasoningResponse = Invoke-RestMethod `
                    -Uri $endpoint `
                    -Method Post `
                    -Headers @{ Authorization = "Bearer $ApiKey"; Accept = 'application/json' } `
                    -ContentType 'application/json; charset=utf-8' `
                    -Body $reasoningBytes `
                    -TimeoutSec $TimeoutSec `
                    -ErrorAction Stop
                $reasoningTimer.Stop()
                $reasoningText = [string]$reasoningResponse.choices[0].message.reasoning_content
                $thinkingTokens = $reasoningResponse.usage.completion_tokens_details.reasoning_tokens
                $reasoningResults += [pscustomobject]@{
                    Mode = $probe.Name; ReasoningEffort = $probe.Effort; Success = $true
                    ReasoningObserved = (-not [string]::IsNullOrWhiteSpace($reasoningText)) -or ([int64]($thinkingTokens -as [int64]) -gt 0)
                    ThinkingTokens = $thinkingTokens
                    ElapsedSeconds = [math]::Round($reasoningTimer.Elapsed.TotalSeconds, 3)
                    RequestId = if ($reasoningResponse.request_id) { $reasoningResponse.request_id } else { $reasoningResponse.id }
                    Error = $null
                }
                Write-Host "$($probe.Name)（reasoning_effort=$($probe.Effort)）成功" -ForegroundColor Green
            }
            catch {
                $reasoningTimer.Stop()
                $reasoningResults += [pscustomobject]@{
                    Mode = $probe.Name; ReasoningEffort = $probe.Effort; Success = $false
                    ReasoningObserved = $false; ThinkingTokens = $null
                    ElapsedSeconds = [math]::Round($reasoningTimer.Elapsed.TotalSeconds, 3)
                    RequestId = $null; Error = (Get-RemoteErrorText $_)
                }
                Write-Host "$($probe.Name)（reasoning_effort=$($probe.Effort)）失败" -ForegroundColor Red
            }
        }
    }
}
finally {
    $ApiKey = $null
    $jsonFile = Join-Path $runDir 'results.json'
    $csvFile = Join-Path $runDir 'results.csv'

    [System.IO.File]::WriteAllText(
        $jsonFile,
        ($results | ConvertTo-Json -Depth 20),
        [System.Text.UTF8Encoding]::new($false)
    )
    $results | Export-Csv -LiteralPath $csvFile -NoTypeInformation -Encoding UTF8

    $successfulVideos = @($results | Where-Object { $_.Stage -eq 'MODEL' -and $_.Success })
    $failedVideos = @($results | Where-Object { $_.Stage -eq 'MODEL' -and -not $_.Success })
    $maxSuccess = $successfulVideos | Sort-Object Bytes -Descending | Select-Object -First 1
    $minLargerFailure = if ($maxSuccess) {
        $failedVideos | Where-Object { $_.Bytes -gt $maxSuccess.Bytes } | Sort-Object Bytes | Select-Object -First 1
    } else { $null }

    $offProbe = $reasoningResults | Where-Object Mode -eq 'off' | Select-Object -First 1
    $onProbe = $reasoningResults | Where-Object Mode -eq 'on' | Select-Object -First 1
    $reasoningVerdict = '未测试'
    if ($offProbe -and $onProbe) {
        if ($offProbe.Success -and $onProbe.Success -and -not $offProbe.ReasoningObserved -and $onProbe.ReasoningObserved) {
            $reasoningVerdict = '支持，且输出侧观察到开关差异'
        }
        elseif ($offProbe.Success -and $onProbe.Success) {
            $reasoningVerdict = '参数均被接受，但输出侧未观察到明确开关差异'
        }
        elseif (-not $offProbe.Success -and $onProbe.Success) {
            $reasoningVerdict = '不支持通过 reasoning_effort=none 关闭'
        }
        else {
            $reasoningVerdict = '无法判定'
        }
    }

    $summary = [ordered]@{
        schema_version = 1
        provider = $providerLabel
        base_url = $BaseUrl
        endpoint = $endpoint
        model = $Model
        generated_at = (Get-Date).ToString('o')
        network_requests_executed = [bool]$Execute
        maximum_successful_video_tier = if ($maxSuccess) { $maxSuccess.Name } else { $null }
        maximum_successful_video_bytes = if ($maxSuccess) { [int64]$maxSuccess.Bytes } else { $null }
        maximum_successful_video_mib = if ($maxSuccess) { [double]$maxSuccess.SizeMiB } else { $null }
        nearest_larger_failed_tier = if ($minLargerFailure) { $minLargerFailure.Name } else { $null }
        nearest_larger_failed_bytes = if ($minLargerFailure) { [int64]$minLargerFailure.Bytes } else { $null }
        reasoning_switch = $reasoningVerdict
        video_results = @($results)
        reasoning_results = @($reasoningResults)
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $runDir 'summary.json'),
        ($summary | ConvertTo-Json -Depth 30),
        [System.Text.UTF8Encoding]::new($false)
    )

    $maxText = if ($maxSuccess) {
        "$($maxSuccess.Name)（$($maxSuccess.Bytes) 字节，$($maxSuccess.SizeMiB) MiB）"
    } else { '无成功档位' }
    $boundaryText = if ($minLargerFailure) {
        "已确认位于 $($maxSuccess.Name) 成功与 $($minLargerFailure.Name) 失败之间"
    } elseif ($maxSuccess) {
        '本轮最大测试档位成功，平台绝对上限仍可能更高'
    } else { '本轮没有形成成功下界' }

    $reportLines = @(
        '# K3 视频输入上限报告', '',
        '## 核心结论', '',
        "- Provider：``$providerLabel``", "- Model：``$Model``",
        "- 接口：``POST $endpoint``（``video_url`` 公网 URL 输入）",
        "- 本轮最大成功视频档位：**$maxText**。",
        "- 边界说明：$boundaryText。",
        "- 独立思考开关：**$reasoningVerdict**。", '',
        '## 视频档位明细', '',
        '|档位|字节数|MiB|成功|阶段|耗时(s)|分类|request_id|',
        '|---|---:|---:|:---:|---|---:|---|---|'
    )
    foreach ($item in $results) {
        $reportLines += "|$($item.Name)|$($item.Bytes)|$($item.SizeMiB)|$(if ($item.Success) {'是'} else {'否'})|$($item.Stage)|$($item.ElapsedSeconds)|$($item.Classification)|$($item.RequestId)|"
    }
    $reportLines += @('', '## 独立思考开关明细', '')
    if ($reasoningResults.Count -gt 0) {
        $reportLines += @('|模式|reasoning_effort|成功|观察到推理输出|思考Token|耗时(s)|request_id|', '|---|---|:---:|:---:|---:|---:|---|')
        foreach ($item in $reasoningResults) {
            $reportLines += "|$($item.Mode)|$($item.ReasoningEffort)|$(if ($item.Success) {'是'} else {'否'})|$(if ($item.ReasoningObserved) {'是'} else {'否'})|$($item.ThinkingTokens)|$($item.ElapsedSeconds)|$($item.RequestId)|"
        }
    } else {
        $reportLines += '- 未执行；正式运行时添加 `-ProbeReasoningSwitch`。'
    }
    $reportLines += @('', '## 口径', '', '- “最大成功档位”仅表示本轮测试集合中成功的最大文件，不自动等同于平台绝对上限。', '- 只有存在更大档位失败时，才能形成该测试矩阵内的成功下界与失败上界。', '- 本测试走 OpenAI 兼容 Chat Completions 的 `video_url`，不是 multipart 本地文件直传。', '- 独立思考关闭以 `reasoning_effort=none` 为探针；参数被接受但没有可观察推理差异时，不宣称已证实关闭。', '')
    [System.IO.File]::WriteAllText(
        (Join-Path $runDir 'benchmark_report.md'),
        ($reportLines -join [Environment]::NewLine),
        [System.Text.UTF8Encoding]::new($false)
    )
}

Write-Host ''
$results | Format-Table Name, Set, SizeMiB, Success, Stage, Classification, ElapsedSeconds, RequestId -AutoSize
Write-Host "Run directory: $runDir"
Write-Host "Report: $(Join-Path $runDir 'benchmark_report.md')"

