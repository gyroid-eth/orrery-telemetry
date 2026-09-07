[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$SpecFile
)
$ErrorActionPreference = 'Stop'
& $Python -X utf8 (Join-Path $PSScriptRoot 'codex_launcher.py') _child --spec-file $SpecFile
exit $LASTEXITCODE
