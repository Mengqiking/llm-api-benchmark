[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Provider,
    [string]$Model = '',
    [string]$ProvidersDir = (Join-Path $PSScriptRoot '..\providers'),
    [int]$Port = 18765,
    [switch]$PreflightOnly,
    [switch]$SkipReasoningSwitch,
    [switch]$ForceReencode
)

$ErrorActionPreference = 'Stop'
$toolkitRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $toolkitRoot '..')).Path
$probeScript = Join-Path $toolkitRoot 'video-limit\Run-ControlledDescendingProbe.ps1'
$completedRoot = Join-Path $workspaceRoot 'completed-tests'
$standalone = -not (Test-Path -LiteralPath $completedRoot)
$cacheRoot = if ($standalone) {
    Join-Path $toolkitRoot '.cache\video-limit'
} else {
    Join-Path $completedRoot 'shared\video-limit-cache'
}
$outputRoot = if ($standalone) {
    Join-Path $toolkitRoot "runs\video-limit\$Provider"
} else {
    Join-Path $completedRoot "providers\$Provider\video-limit-runs"
}

$candidateFiles = @(
    (Join-Path $ProvidersDir "$Provider.json"),
    (Join-Path $toolkitRoot "providers\$Provider.json")
)
$providerFile = $candidateFiles | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $providerFile) {
    throw "Provider '$Provider' not found under '$ProvidersDir' or '$toolkitRoot\providers'."
}

$config = Get-Content -LiteralPath $providerFile -Raw | ConvertFrom-Json
if ($config.protocols -notcontains 'openai') {
    throw "Provider '$Provider' does not enable the OpenAI-compatible protocol required by video_url."
}
if ([string]::IsNullOrWhiteSpace($Model)) {
    $Model = [string]$config.default_model
}

$baseUrl = ([string]$config.base_url).TrimEnd('/')
if ($baseUrl -match '/v1$') {
    $baseUrl = $baseUrl.Substring(0, $baseUrl.Length - 3)
}

$apiKey = $null
if (-not $PreflightOnly) {
    $envName = [string]$config.api_key_env
    $envItem = Get-Item -LiteralPath "Env:$envName" -ErrorAction SilentlyContinue
    if (-not $envItem -or [string]::IsNullOrWhiteSpace($envItem.Value)) {
        throw "Set environment variable $envName before a billable run."
    }
    $apiKey = $envItem.Value
}

try {
    & $probeScript `
        -ApiKey $apiKey `
        -BaseUrl $baseUrl `
        -Model $Model `
        -ProviderName ([string]$config.display_name) `
        -Port $Port `
        -GeneratedDirectory (Join-Path $cacheRoot 'real-generated') `
        -ToolDirectory (Join-Path $cacheRoot 'tools') `
        -RuntimeDirectory (Join-Path $cacheRoot 'runtime') `
        -OutputRoot $outputRoot `
        -PreflightOnly:$PreflightOnly `
        -SkipReasoningSwitch:$SkipReasoningSwitch `
        -ForceReencode:$ForceReencode
}
finally {
    $apiKey = $null
}
