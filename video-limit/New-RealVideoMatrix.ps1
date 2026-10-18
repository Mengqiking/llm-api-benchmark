[CmdletBinding()]
param(
    [double[]]$TargetMiB = @(100, 90, 80, 70, 60, 50),
    [int]$DurationSeconds = 60,
    [string]$OutputDirectory,
    [string]$FfmpegPath = '',
    [switch]$ForceReencode
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $PSScriptRoot 'real-generated'
}
$ffmpeg = $FfmpegPath
if (-not $ffmpeg) {
    $ffmpeg = Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'tools') -Recurse -Filter ffmpeg.exe -File -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}

if (-not $ffmpeg) { throw 'Portable ffmpeg was not found under tools\ffmpeg-btbn.' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

foreach ($sizeMiB in $TargetMiB) {
    $output = Join-Path $OutputDirectory "real-video-$sizeMiB-MiB.mp4"
    $targetBytes = [int64][math]::Round($sizeMiB * 1MB)
    $targetBitsPerSecond = ($sizeMiB * 1MB * 8) / $DurationSeconds
    $videoKbps = [math]::Floor(($targetBitsPerSecond - 128000 - 45000) / 1000)
    $iteration = 0

    if (-not $ForceReencode -and (Test-Path -LiteralPath $output)) {
        $existing = Get-Item -LiteralPath $output
        if ([math]::Abs($existing.Length - $targetBytes) -le 262144) {
            Write-Host "Reusing target $sizeMiB MiB: $($existing.FullName)"
            $sha256 = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
            [pscustomobject]@{
                TargetMiB = $sizeMiB
                ActualMiB = [math]::Round($existing.Length / 1MB, 3)
                Bytes = $existing.Length
                VideoKbps = $videoKbps
                SHA256 = $sha256
                FullName = $existing.FullName
            }
            continue
        }
    }

    do {
        $iteration++
        Write-Host "Encoding target $sizeMiB MiB, pass $iteration, video bitrate ${videoKbps}k"

        $arguments = @(
            '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi',
            '-i', "testsrc2=size=640x480:rate=25:duration=$DurationSeconds,noise=alls=30:allf=t+u:all_seed=42",
            '-f', 'lavfi',
            '-i', "sine=frequency=1000:sample_rate=44100:duration=$DurationSeconds",
            '-map', '0:v:0', '-map', '1:a:0',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-profile:v', 'high',
            '-pix_fmt', 'yuv420p', '-r', '25', '-g', '50',
            '-b:v', "${videoKbps}k", '-minrate', "${videoKbps}k", '-maxrate', "${videoKbps}k",
            '-bufsize', "$($videoKbps * 2)k",
            '-x264-params', 'nal-hrd=cbr:force-cfr=1',
            '-c:a', 'aac', '-b:a', '128k', '-ac', '1',
            '-t', $DurationSeconds, '-movflags', '+faststart',
            $output
        )

        & $ffmpeg @arguments
        if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed for $sizeMiB MiB." }

        $actualBytes = (Get-Item -LiteralPath $output).Length
        $difference = [math]::Abs($actualBytes - $targetBytes)
        if ($difference -le 104858) { break }

        # Remove the approximately fixed AAC/container contribution before
        # adjusting the H.264 bitrate. This converges in two or three passes.
        $fixedBytes = 1000000
        $videoKbps = [math]::Max(100, [math]::Round(
            $videoKbps * (($targetBytes - $fixedBytes) / ($actualBytes - $fixedBytes))
        ))
    } while ($iteration -lt 3)

    $item = Get-Item -LiteralPath $output
    if ([math]::Abs($item.Length - $targetBytes) -gt 262144) {
        throw "Final size for $sizeMiB MiB is outside the 0.25 MiB tolerance: $($item.Length) bytes."
    }

    $sha256 = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
    [pscustomobject]@{
        TargetMiB = $sizeMiB
        ActualMiB = [math]::Round($item.Length / 1MB, 3)
        Bytes = $item.Length
        VideoKbps = $videoKbps
        SHA256 = $sha256
        FullName = $item.FullName
    }
}
