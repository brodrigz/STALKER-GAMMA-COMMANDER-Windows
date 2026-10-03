param([switch]$SkipPython, [string]$CliSource, [switch]$ForceCliBuild)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$manifest = Get-Content -LiteralPath (Join-Path $repoRoot 'cli/windows-backend.json') -Raw | ConvertFrom-Json
& (Join-Path $PSScriptRoot 'Build-Cli.ps1') -CliSource $CliSource -Force:$ForceCliBuild
if (-not $SkipPython) {
    $python = Join-Path $repoRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        & py -3.10 -m venv (Join-Path $repoRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.10 x64 and rerun setup.' }
    }
    & $python -m pip install -r (Join-Path $repoRoot 'requirements-dev.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
}
Write-Host "Windows backend $($manifest.version) ready. Start Commander with .\run.ps1."
