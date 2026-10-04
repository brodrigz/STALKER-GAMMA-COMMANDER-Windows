<#
.SYNOPSIS
Build and smoke-test a portable Windows ZIP in a timestamped release folder.
.DESCRIPTION
Packages the current launcher source and the CLI revision pinned in
cli/windows-backend.json. Includes release metadata and SHA-256 checksums.
.PARAMETER OutputDirectory
Optional destination. Defaults to dist/releases/portable-<timestamp>.
#>
param([string]$OutputDirectory)

$ErrorActionPreference = 'Stop'
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $PSScriptRoot ('dist/releases/portable-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
}

& (Join-Path $PSScriptRoot 'scripts/Build-Windows.ps1') -OutputDirectory $OutputDirectory
