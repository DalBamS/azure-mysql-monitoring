<#
.SYNOPSIS
    Loads testing/.env into the current PowerShell session.

.DESCRIPTION
    Dot-source this so the variables persist in your shell:

        . ./scripts/load-env.ps1

    Running it normally (without the leading dot) sets variables in a child scope that
    disappears the moment the script exits — which looks like the script silently did
    nothing.
#>
[CmdletBinding()]
param(
    [string]$EnvFile
)

$ErrorActionPreference = 'Stop'

if (-not $EnvFile) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $EnvFile = Join-Path (Split-Path -Parent $scriptDir) '.env'
}

if (-not (Test-Path $EnvFile)) {
    throw "No environment file at $EnvFile. Run deploy.ps1 first."
}

$loaded = 0
foreach ($line in Get-Content $EnvFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }

    $split = $trimmed.IndexOf('=')
    if ($split -lt 1) { continue }

    $name = $trimmed.Substring(0, $split).Trim()
    $value = $trimmed.Substring($split + 1).Trim()

    Set-Item -Path "Env:$name" -Value $value
    $loaded++

    # Never echo the password, even to an interactive terminal — shells keep scrollback.
    $shown = if ($name -eq 'MYSQL_PASSWORD') { '***redacted***' } else { $value }
    Write-Host ("  {0,-28} = {1}" -f $name, $shown)
}

Write-Host "`nLoaded $loaded variables from $EnvFile" -ForegroundColor Green
