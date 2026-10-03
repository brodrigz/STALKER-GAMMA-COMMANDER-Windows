param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AppArguments)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run .\scripts\Setup-Windows.ps1 first to prepare the Windows dependencies.'
}
Push-Location $PSScriptRoot
try {
    & $python -m commander_gui @AppArguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
