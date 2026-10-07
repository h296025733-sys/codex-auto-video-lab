[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$StudioArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspaceRoot = $PSScriptRoot
$pythonPath = Join-Path $workspaceRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Workspace Python is missing: $pythonPath"
}

& $pythonPath -m auto_video_lab.cli @StudioArgs
exit $LASTEXITCODE
