[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$ffmpegPath = Join-Path $workspaceRoot 'tools\ffmpeg\bin\ffmpeg.exe'
$ffprobePath = Join-Path $workspaceRoot 'tools\ffmpeg\bin\ffprobe.exe'
$artifactDir = Join-Path $workspaceRoot 'tests\artifacts'
$outputPath = Join-Path $artifactDir 'synthetic-vertical-smoke.mp4'

foreach ($required in @($ffmpegPath, $ffprobePath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Missing workspace-local tool: $required"
    }
}

New-Item -ItemType Directory -Path $artifactDir -Force | Out-Null

$ffmpegArguments = @(
    '-hide_banner',
    '-loglevel', 'error',
    '-y',
    '-f', 'lavfi',
    '-i', 'testsrc2=size=720x1280:rate=30:duration=2',
    '-f', 'lavfi',
    '-i', 'sine=frequency=1000:sample_rate=48000:duration=2',
    '-c:v', 'libx264',
    '-pix_fmt', 'yuv420p',
    '-c:a', 'aac',
    '-shortest',
    $outputPath
)

& $ffmpegPath @ffmpegArguments
if ($LASTEXITCODE -ne 0) {
    throw "Synthetic FFmpeg render failed with exit code $LASTEXITCODE"
}

$probeText = & $ffprobePath -v error -show_entries 'format=duration:stream=codec_type,codec_name,width,height' -of json $outputPath
if ($LASTEXITCODE -ne 0) {
    throw "Synthetic FFprobe inspection failed with exit code $LASTEXITCODE"
}

$probe = $probeText | ConvertFrom-Json
$videoStream = $probe.streams | Where-Object { $_.codec_type -eq 'video' } | Select-Object -First 1
$audioStream = $probe.streams | Where-Object { $_.codec_type -eq 'audio' } | Select-Object -First 1
$duration = [double]::Parse($probe.format.duration, [Globalization.CultureInfo]::InvariantCulture)

$passed = $null -ne $videoStream -and
    $null -ne $audioStream -and
    $videoStream.width -eq 720 -and
    $videoStream.height -eq 1280 -and
    $duration -ge 1.9 -and
    $duration -le 2.1

[ordered]@{
    status = if ($passed) { 'ISOLATED_SELF_TEST_PASS' } else { 'ISOLATED_SELF_TEST_FAIL' }
    scope = 'synthetic 720x1280 H.264/AAC generation and ffprobe inspection using workspace-local FFmpeg only'
    artifact = $outputPath
    observed = [ordered]@{
        durationSeconds = $duration
        videoCodec = $videoStream.codec_name
        width = $videoStream.width
        height = $videoStream.height
        audioCodec = $audioStream.codec_name
    }
    notTested = @(
        'user media ingestion',
        'speech transcription',
        'scene detection',
        'semantic clip selection',
        'caption layout',
        'editor integration',
        'publishing'
    )
} | ConvertTo-Json -Depth 5

if (-not $passed) {
    exit 1
}
