param(
    [string]$ProjectDir = '',
    [string]$SourceHome = '',
    [string]$StateDir = '',
    [int]$Port = 5330
)
$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$consoleArguments = @('-m', 'agentwatch')
if ($SourceHome) { $consoleArguments += @('--home', $SourceHome) }
$consoleArguments += @('console', '--port', $Port)
if ($ProjectDir) { $consoleArguments += @('--project-dir', $ProjectDir) }
if ($StateDir) { $consoleArguments += @('--state-dir', $StateDir) }
Push-Location -LiteralPath $repository
try { & python @consoleArguments; exit $LASTEXITCODE }
finally { Pop-Location }
