[CmdletBinding()]
param(
    [string]$Project = (Get-Location).Path,
    [string]$StateDirectory = (Join-Path $env:LOCALAPPDATA 'orrery-telemetry\local'),
    [string]$PythonCommand = '',
    [int]$MailPort = 18765,
    [int]$DashboardPort = 8770,
    [switch]$DryRun,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (-not $PythonCommand) { $PythonCommand = Join-Path $repo '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PythonCommand -PathType Leaf)) {
    throw 'Create the repository .venv using CONTRIBUTING.md, or pass -PythonCommand with an existing development interpreter.'
}
$arguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'windows_local.py'),
    '--project', $Project, '--state-directory', $StateDirectory,
    '--mail-port', $MailPort, '--dashboard-port', $DashboardPort)
if ($DryRun) { $arguments += '--dry-run' }
if ($NoBrowser) { $arguments += '--no-browser' }
& $PythonCommand @arguments
exit $LASTEXITCODE
