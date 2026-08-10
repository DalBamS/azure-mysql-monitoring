<#
.SYNOPSIS
    Provisions data sources and dashboards from this repository into Azure Managed Grafana.

.DESCRIPTION
    Reads the committed JSON under ../datasources/ and ../dashboards/, substitutes ${ENV_VAR}
    placeholders from the current environment, and applies them via `az grafana`.

    Nothing secret is substituted. Both data sources authenticate with the workspace's managed
    identity, so the only values injected are cluster URIs and IDs. This is what keeps the
    committed files safe to review in a pull request.

    Re-running is safe: existing data sources are updated in place, keeping their uid stable so
    dashboard JSON stays portable across environments.

.EXAMPLE
    . ../../testing/scripts/load-env.ps1
    ./deploy.ps1 -GrafanaName mysqlmon-grafana-toqai -ResourceGroup mysql-mon-test

.EXAMPLE
    # Data sources only, skipping dashboards
    ./deploy.ps1 -GrafanaName my-grafana -ResourceGroup my-rg -SkipDashboards
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GrafanaName,

    [Parameter(Mandatory = $true)]
    [string]$ResourceGroup,

    [string]$Folder = 'MySQL',

    [switch]$SkipDatasources,

    [switch]$SkipDashboards
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$grafanaDir = Split-Path -Parent $scriptDir

# The CLI prompts before installing a missing extension; with no TTY that surfaces as
# "EOF when reading a line" wrapped in a traceback, which reads like a broken command.
$env:AZURE_EXTENSION_USE_DYNAMIC_INSTALL = 'yes_without_prompt'

function Write-Step($message) {
    Write-Host "`n=== $message ===" -ForegroundColor Cyan
}

# testing/.env does not carry the subscription, because the deployment already knew it implicitly.
# Fall back to the logged-in context rather than failing on a value that is always available.
if ([string]::IsNullOrWhiteSpace($env:AZURE_SUBSCRIPTION_ID)) {
    $env:AZURE_SUBSCRIPTION_ID = az account show --query id -o tsv
}

function Expand-Placeholders {
    param([string]$Content, [string]$Source)

    # Only ${VAR} is expanded, never $VAR, so a Kusto expression or a Grafana template
    # variable such as $run_id passes through untouched.
    $missing = @()
    $expanded = [regex]::Replace($Content, '\$\{([A-Z0-9_]+)\}', {
        param($match)
        $name = $match.Groups[1].Value
        $value = [Environment]::GetEnvironmentVariable($name)
        if ([string]::IsNullOrWhiteSpace($value)) {
            $script:missingVars += $name
            return $match.Value
        }
        return $value
    })

    if ($script:missingVars.Count -gt 0) {
        $names = ($script:missingVars | Sort-Object -Unique) -join ', '
        throw "$Source needs these environment variables: $names. Run: . ./testing/scripts/load-env.ps1"
    }
    return $expanded
}

# --- Data sources ----------------------------------------------------------------------
if (-not $SkipDatasources) {
    Write-Step 'Data sources'

    $existing = az grafana data-source list --name $GrafanaName --resource-group $ResourceGroup `
        --query '[].{uid:uid, name:name}' -o json | ConvertFrom-Json
    $existingUids = @($existing | ForEach-Object { $_.uid })
    $existingNames = @($existing | ForEach-Object { $_.name })

    foreach ($file in Get-ChildItem (Join-Path $grafanaDir 'datasources') -Filter *.json) {
        $script:missingVars = @()
        $definition = Expand-Placeholders -Content (Get-Content $file.FullName -Raw) -Source $file.Name
        $parsed = $definition | ConvertFrom-Json
        $uid = $parsed.uid

        # Grafana enforces uniqueness on name as well as uid, and a uid can never be changed
        # after creation. Matching on either avoids a 409 against Azure Managed Grafana's
        # built-in "Azure Monitor" data source, which we adopt rather than duplicate.
        if ($existingNames -contains $parsed.name -and $existingUids -notcontains $uid) {
            throw ("A data source named '$($parsed.name)' already exists with a different uid. " +
                   "uid is immutable in Grafana; either delete it or align $($file.Name) to its uid.")
        }

        $temp = New-TemporaryFile
        try {
            Set-Content -Path $temp -Value $definition -Encoding utf8

            if ($existingUids -contains $uid) {
                az grafana data-source update --name $GrafanaName --resource-group $ResourceGroup `
                    --data-source $uid --definition "@$temp" --output none
                $verb = 'updated'
            } else {
                az grafana data-source create --name $GrafanaName --resource-group $ResourceGroup `
                    --definition "@$temp" --output none
                $verb = 'created'
            }
            if ($LASTEXITCODE -ne 0) { throw "Failed to apply $($file.Name)." }
            Write-Host "  $verb $($file.Name) (uid=$uid)"
        } finally {
            Remove-Item $temp -ErrorAction SilentlyContinue
        }
    }
}

# --- Dashboards ------------------------------------------------------------------------
if (-not $SkipDashboards) {
    Write-Step "Dashboards -> folder '$Folder'"

    $folders = az grafana folder list --name $GrafanaName --resource-group $ResourceGroup `
        --query '[].title' -o json | ConvertFrom-Json
    if ($folders -notcontains $Folder) {
        az grafana folder create --name $GrafanaName --resource-group $ResourceGroup `
            --title $Folder --output none
        Write-Host "  created folder '$Folder'"
    }

    foreach ($file in Get-ChildItem (Join-Path $grafanaDir 'dashboards') -Filter *.json) {
        $script:missingVars = @()
        $definition = Expand-Placeholders -Content (Get-Content $file.FullName -Raw) -Source $file.Name

        $temp = New-TemporaryFile
        try {
            Set-Content -Path $temp -Value $definition -Encoding utf8
            # --overwrite makes the repo the source of truth: a dashboard edited in the browser
            # is replaced on the next deploy, matching allowUiUpdates:false in dashboards.yaml.
            az grafana dashboard create --name $GrafanaName --resource-group $ResourceGroup `
                --folder $Folder --definition "@$temp" --overwrite --output none
            if ($LASTEXITCODE -ne 0) { throw "Failed to apply $($file.Name)." }
            Write-Host "  applied $($file.Name)"
        } finally {
            Remove-Item $temp -ErrorAction SilentlyContinue
        }
    }
}

$endpoint = az grafana show --name $GrafanaName --resource-group $ResourceGroup `
    --query properties.endpoint -o tsv
Write-Step 'Done'
Write-Host $endpoint -ForegroundColor Green
