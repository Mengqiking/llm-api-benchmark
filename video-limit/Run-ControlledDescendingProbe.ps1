[CmdletBinding()]
param(
    [string]$ApiKey = $env:ONELINK_API_KEY,
    [string]$BaseUrl = 'https://api.onelinkai.cloud',
    [string]$Model = 'kimi-k3',
    [string]$ProviderName = '',
    [int]$Port = 18765,
    [string]$GeneratedDirectory = '',
    [string]$ToolDirectory = '',
    [string]$RuntimeDirectory = '',
    [string]$OutputRoot = '',
    [switch]$PreflightOnly,
    [switch]$SkipReasoningSwitch,
    [switch]$ForceReencode
)

$ErrorActionPreference = 'Stop'

if (-not $PreflightOnly -and [string]::IsNullOrWhiteSpace($ApiKey)) {
    throw 'Set ONELINK_API_KEY or pass -ApiKey.'
}

$generatedDir = if ($GeneratedDirectory) { $GeneratedDirectory } else { Join-Path $PSScriptRoot 'real-generated' }
$toolDir = if ($ToolDirectory) { $ToolDirectory } else { Join-Path $PSScriptRoot 'tools' }
$runtimeDir = if ($RuntimeDirectory) { $RuntimeDirectory } else { Join-Path $PSScriptRoot 'runtime' }
$probeOutputRoot = if ($OutputRoot) { $OutputRoot } else { Join-Path $PSScriptRoot 'runs' }
$cloudflared = Join-Path $toolDir 'cloudflared.exe'
$generator = Join-Path $PSScriptRoot 'New-RealVideoMatrix.ps1'
$probe = Join-Path $PSScriptRoot 'video-size-probe.ps1'

New-Item -ItemType Directory -Path $generatedDir, $toolDir, $runtimeDir -Force | Out-Null

if (-not (Test-Path -LiteralPath $cloudflared)) {
    Write-Host 'Downloading portable cloudflared...'
    & curl.exe -L --fail --retry 2 -o $cloudflared `
        'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to download cloudflared.' }
}

$ffmpeg = Get-ChildItem -LiteralPath $toolDir -Recurse -Filter ffmpeg.exe -File -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $ffmpeg) {
    Write-Host 'Downloading portable ffmpeg...'
    $ffmpegZip = Join-Path $toolDir 'ffmpeg-windows-gpl.zip'
    & curl.exe -L --fail --retry 2 -o $ffmpegZip `
        'https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to download ffmpeg.' }
    Expand-Archive -LiteralPath $ffmpegZip -DestinationPath (Join-Path $toolDir 'ffmpeg') -Force
    $ffmpeg = Get-ChildItem -LiteralPath $toolDir -Recurse -Filter ffmpeg.exe -File |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $ffmpeg) { throw 'ffmpeg.exe was not found after extraction.' }
}

Write-Host 'Preparing real encoded 100/90/80/70/60/50 MiB videos...'
$matrix = @(& $generator -TargetMiB @(100, 90, 80, 70, 60, 50) -OutputDirectory $generatedDir -FfmpegPath $ffmpeg -ForceReencode:$ForceReencode)
$matrix | Format-Table TargetMiB, ActualMiB, Bytes, VideoKbps, SHA256 -AutoSize
$matrix | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $runtimeDir 'real-video-matrix.json') -Encoding UTF8

$httpOut = Join-Path $runtimeDir 'http.out.log'
$httpErr = Join-Path $runtimeDir 'http.err.log'
$tunnelOut = Join-Path $runtimeDir 'tunnel.out.log'
$tunnelErr = Join-Path $runtimeDir 'tunnel.err.log'
Remove-Item -LiteralPath $httpOut, $httpErr, $tunnelOut, $tunnelErr -Force -ErrorAction SilentlyContinue

$httpProcess = $null
$tunnelProcess = $null

try {
    $httpProcess = Start-Process `
        -FilePath 'python' `
        -ArgumentList @('-m', 'http.server', $Port, '--bind', '127.0.0.1', '--directory', $generatedDir) `
        -WindowStyle Hidden `
        -RedirectStandardOutput $httpOut `
        -RedirectStandardError $httpErr `
        -PassThru

    $local100 = Get-Item -LiteralPath (Join-Path $generatedDir 'real-video-100-MiB.mp4')
    $localUrl = "http://127.0.0.1:$Port/real-video-100-MiB.mp4"
    $deadline = (Get-Date).AddSeconds(30)
    $localReady = $false
    do {
        try {
            $localHead = Invoke-WebRequest -Uri $localUrl -Method Head -UseBasicParsing -TimeoutSec 3
            $localReady = $localHead.StatusCode -eq 200
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    } while (-not $localReady -and (Get-Date) -lt $deadline)

    if (-not $localReady) { throw 'Local HTTP server did not become ready.' }

    $publicBaseUrl = $null

    for ($attempt = 1; $attempt -le 3 -and -not $publicBaseUrl; $attempt++) {
        $attemptOut = Join-Path $runtimeDir "tunnel-$attempt.out.log"
        $attemptErr = Join-Path $runtimeDir "tunnel-$attempt.err.log"
        Remove-Item -LiteralPath $attemptOut, $attemptErr -Force -ErrorAction SilentlyContinue

        Write-Host "Waiting for the temporary HTTPS tunnel (attempt $attempt/3)..."
        $tunnelProcess = Start-Process `
            -FilePath $cloudflared `
            -ArgumentList @('tunnel', '--url', "http://127.0.0.1:$Port", '--no-autoupdate', '--loglevel', 'info') `
            -WindowStyle Hidden `
            -RedirectStandardOutput $attemptOut `
            -RedirectStandardError $attemptErr `
            -PassThru

        $deadline = (Get-Date).AddSeconds(45)
        do {
            Start-Sleep -Seconds 1
            $logText = ''
            if (Test-Path -LiteralPath $attemptOut) { $logText += Get-Content -LiteralPath $attemptOut -Raw }
            if (Test-Path -LiteralPath $attemptErr) { $logText += Get-Content -LiteralPath $attemptErr -Raw }

            if ($logText -match 'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com') {
                $publicBaseUrl = $Matches[0]
            }
        } while (-not $publicBaseUrl -and -not $tunnelProcess.HasExited -and (Get-Date) -lt $deadline)

        if (-not $publicBaseUrl -and -not $tunnelProcess.HasExited) {
            Stop-Process -Id $tunnelProcess.Id -ErrorAction SilentlyContinue
            $tunnelProcess.WaitForExit(5000)
        }
    }

    if (-not $publicBaseUrl) { throw 'Cloudflare Quick Tunnel URL was not created.' }

    $publicTestUrl = "$publicBaseUrl/real-video-100-MiB.mp4"
    $deadline = (Get-Date).AddSeconds(60)
    $publicReady = $false

    do {
        try {
            $publicHead = Invoke-WebRequest -Uri $publicTestUrl -Method Head -UseBasicParsing -TimeoutSec 10
            $publicReady = (
                $publicHead.StatusCode -eq 200 -and
                [int64](@($publicHead.Headers['Content-Length'])[0]) -eq $local100.Length -and
                [string](@($publicHead.Headers['Content-Type'])[0]) -match '^video/mp4'
            )
        }
        catch {
            Start-Sleep -Seconds 1
        }
    } while (-not $publicReady -and (Get-Date) -lt $deadline)

    if (-not $publicReady) { throw 'The public 100 MiB URL did not pass HEAD validation.' }

    Write-Host "Public base URL: $publicBaseUrl" -ForegroundColor Green

    $sizes = @(100, 90, 80, 70, 60, 50)
    $manifestItems = foreach ($size in $sizes) {
        $localFile = Get-Item -LiteralPath (Join-Path $generatedDir "real-video-$size-MiB.mp4")
        [pscustomobject]@{
            name = "RealVideo-${size}MiB"
            set = 'real-video'
            expectedBytes = [int64]$localFile.Length
            url = "$publicBaseUrl/real-video-$size-MiB.mp4"
            notes = 'Real 60-second H.264/AAC media payload; 640x480, 25 fps, deterministic high-motion source.'
        }
    }

    $manifestPath = Join-Path $runtimeDir 'controlled-clone-manifest.json'
    [System.IO.File]::WriteAllText(
        $manifestPath,
        ($manifestItems | ConvertTo-Json -Depth 10),
        [System.Text.UTF8Encoding]::new($false)
    )

    if ($PreflightOnly) {
        Write-Host 'Running public URL preflight only; no model requests will be made.'
        & $probe `
            -BaseUrl $BaseUrl `
            -Model $Model `
            -ProviderName $ProviderName `
            -Manifest $manifestPath `
            -Set all `
            -Order descending `
            -OutputRoot $probeOutputRoot
    }
    else {
        Write-Host 'Starting controlled descending probe: 100 -> 90 -> 80 -> 70 -> 60 -> 50 MiB'
        & $probe `
            -ApiKey $ApiKey `
            -BaseUrl $BaseUrl `
            -Model $Model `
            -ProviderName $ProviderName `
            -Manifest $manifestPath `
            -Set all `
            -Order descending `
            -Execute `
            -ContinueAfterSuccess `
            -ProbeReasoningSwitch:(-not $SkipReasoningSwitch) `
            -OutputRoot $probeOutputRoot
    }
}
finally {
    $ApiKey = $null
    if ($tunnelProcess -and -not $tunnelProcess.HasExited) {
        Stop-Process -Id $tunnelProcess.Id -ErrorAction SilentlyContinue
    }
    if ($httpProcess -and -not $httpProcess.HasExited) {
        Stop-Process -Id $httpProcess.Id -ErrorAction SilentlyContinue
    }
    Write-Host 'Temporary HTTP server and tunnel stopped.'
}
