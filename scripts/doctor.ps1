[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $workspaceRoot 'config\toolchain.json'

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string]$Arguments
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $Arguments
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Unable to start: $FilePath"
    }

    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    if ($process.ExitCode -ne 0) {
        throw "Command failed with exit code $($process.ExitCode): $FilePath $Arguments`n$stderr"
    }

    return (($stdout + "`n" + $stderr) -split "`r?`n" | Where-Object { $_ } | Select-Object -First 1)
}

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Toolchain manifest is missing: $manifestPath"
}

$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$pythonPath = Join-Path $workspaceRoot $manifest.python.executable
$ffmpegPath = Join-Path $workspaceRoot $manifest.ffmpeg.ffmpegPath
$ffprobePath = Join-Path $workspaceRoot $manifest.ffmpeg.ffprobePath
$modelRoot = Join-Path $workspaceRoot 'models\faster-whisper-small'

$checks = [ordered]@{}

foreach ($item in @(
    @{ Name = 'python'; Path = $pythonPath },
    @{ Name = 'ffmpeg'; Path = $ffmpegPath },
    @{ Name = 'ffprobe'; Path = $ffprobePath }
)) {
    $checks[$item.Name] = [ordered]@{
        path = $item.Path
        exists = Test-Path -LiteralPath $item.Path -PathType Leaf
    }
}

if ($checks.ffmpeg.exists) {
    $checks.ffmpeg.sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ffmpegPath).Hash.ToLowerInvariant()
    $checks.ffmpeg.hashMatches = $checks.ffmpeg.sha256 -eq $manifest.ffmpeg.ffmpegSha256
    $checks.ffmpeg.versionLine = Invoke-NativeCapture -FilePath $ffmpegPath -Arguments '-version'
}

if ($checks.ffprobe.exists) {
    $checks.ffprobe.sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ffprobePath).Hash.ToLowerInvariant()
    $checks.ffprobe.hashMatches = $checks.ffprobe.sha256 -eq $manifest.ffmpeg.ffprobeSha256
    $checks.ffprobe.versionLine = Invoke-NativeCapture -FilePath $ffprobePath -Arguments '-version'
}

if ($checks.python.exists) {
    $checks.python.versionLine = Invoke-NativeCapture -FilePath $pythonPath -Arguments '--version'
    $checks.python.mediaPackageVersions = Invoke-NativeCapture -FilePath $pythonPath -Arguments '-c "import av, ctranslate2, faster_whisper; print(f''faster-whisper={faster_whisper.__version__};ctranslate2={ctranslate2.__version__};av={av.__version__}'')"'
}

$requiredModelFiles = @('config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt')
$modelFileChecks = [ordered]@{}
foreach ($filename in $requiredModelFiles) {
    $path = Join-Path $modelRoot $filename
    $modelFileChecks[$filename] = [ordered]@{
        path = $path
        exists = Test-Path -LiteralPath $path -PathType Leaf
        bytes = if (Test-Path -LiteralPath $path -PathType Leaf) { (Get-Item -LiteralPath $path).Length } else { 0 }
    }
}
$missingModelFiles = @($requiredModelFiles | Where-Object { -not $modelFileChecks[$_].exists })
$modelReady = $missingModelFiles.Count -eq 0
$checks.transcriptionModel = [ordered]@{
    path = $modelRoot
    ready = $modelReady
    files = $modelFileChecks
}

$ready = $checks.python.exists -and
    $checks.ffmpeg.exists -and
    $checks.ffprobe.exists -and
    $checks.ffmpeg.hashMatches -and
    $checks.ffprobe.hashMatches -and
    $modelReady

[ordered]@{
    status = if ($ready) { 'READY_AUTOMATION_CORE' } else { 'NOT_READY' }
    scope = 'workspace-local executable presence, Python media-package imports, FFmpeg binary hashes, and offline transcription-model file presence only'
    workspace = $workspaceRoot
    checks = $checks
    notTested = @(
        'runtime media analysis',
        'runtime speech transcription',
        'runtime timeline generation',
        'runtime video rendering',
        'semantic edit quality',
        'publishing'
    )
} | ConvertTo-Json -Depth 6

if (-not $ready) {
    exit 1
}
