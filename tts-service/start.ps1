param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Config
)
$ErrorActionPreference = 'Stop'
$configPath = (Resolve-Path -LiteralPath $Config).Path
& $Python (Join-Path $PSScriptRoot 'run_server.py') --config $configPath
exit $LASTEXITCODE
