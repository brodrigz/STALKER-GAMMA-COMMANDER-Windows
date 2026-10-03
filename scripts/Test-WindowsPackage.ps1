param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Executable')][string]$ExecutablePath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Archive')][string]$PortableArchive
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if ($PortableArchive) {
    # Relocate the release, including paths Windows argument quoting often breaks.
    $destination = Join-Path $repoRoot ("build/windows/portable São Paulo/" + [guid]::NewGuid().ToString('N'))
    Expand-Archive -LiteralPath $PortableArchive -DestinationPath $destination
    & $PSCommandPath -ExecutablePath (Join-Path $destination 'STALKER-GAMMA-COMMANDER/STALKER-GAMMA-COMMANDER.exe')
    return
}
$ExecutablePath = (Resolve-Path -LiteralPath $ExecutablePath).Path
$reportDirectory = Join-Path $repoRoot 'build/windows/smoke reports'
New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
$previousAppData = $env:APPDATA
$previousLocalAppData = $env:LOCALAPPDATA
$previousCli = $env:STALKER_GAMMA_CLI
$previousPythonPath = $env:PYTHONPATH
$previousPythonHome = $env:PYTHONHOME
$previousReset = $env:PYINSTALLER_RESET_ENVIRONMENT
try {
    # A packaged app must work without the checkout, venv, or a CLI override.
    $env:APPDATA = Join-Path $reportDirectory 'roaming'
    $env:LOCALAPPDATA = Join-Path $reportDirectory 'local'
    $env:STALKER_GAMMA_CLI = $null
    $env:PYTHONPATH = $null
    $env:PYTHONHOME = $null
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    foreach ($component in @('commander', 'assistant')) {
        $report = Join-Path $reportDirectory "$component.json"
        if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report }
        $arguments = @('--packaging-smoke-test', ('"{0}"' -f $report))
        if ($component -eq 'assistant') { $arguments += '--assistant' }
        $process = Start-Process -FilePath $ExecutablePath -ArgumentList $arguments -WorkingDirectory $reportDirectory -WindowStyle Hidden -PassThru
        if (-not $process.WaitForExit(45000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            throw "Packaged $component smoke test timed out."
        }
        if ($process.ExitCode -ne 0) {
            $detail = if (Test-Path -LiteralPath $report) { Get-Content -LiteralPath $report -Raw } else { 'No report was produced. Check Windows loader errors.' }
            throw "Packaged $component failed (exit $($process.ExitCode)): $detail"
        }
        $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
        if (-not $result.ok) { throw "Packaged $component validation failed: $($result.error)" }
        Write-Host "Packaged $component passed: $($result.checks -join ', ')"
    }
} finally {
    $env:APPDATA = $previousAppData
    $env:LOCALAPPDATA = $previousLocalAppData
    $env:STALKER_GAMMA_CLI = $previousCli
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONHOME = $previousPythonHome
    $env:PYINSTALLER_RESET_ENVIRONMENT = $previousReset
}
