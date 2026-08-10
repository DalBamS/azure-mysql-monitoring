<#
.SYNOPSIS
    Deploys the azure-mysql-monitoring test environment to real Azure resources.

.DESCRIPTION
    Creates a resource group and deploys MySQL Flexible Server 8.4, Log Analytics with
    diagnostic settings, and optionally Azure Data Explorer and Azure Managed Grafana.

    On success it writes testing/.env with the environment variables the collector and
    verify.py expect. That file contains the MySQL password and is git-ignored.

    THIS COSTS MONEY. The ADX cluster and the Grafana workspace bill while idle.
    Run teardown.ps1 when finished.

.EXAMPLE
    ./deploy.ps1 -ResourceGroup mysql-mon-test -Location koreacentral

.EXAMPLE
    # Cheapest useful run: MySQL + Log Analytics only
    ./deploy.ps1 -ResourceGroup mysql-mon-test -SkipAdx -SkipGrafana
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroup,

    [string]$Location = 'koreacentral',

    [string]$NamePrefix = 'mysqlmon',

    [string]$AdminLogin = 'mysqladmin',

    [string]$DatabaseName = 'monitoring_test',

    [switch]$SkipAdx,

    [switch]$SkipGrafana,

    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$testingDir = Split-Path -Parent $scriptDir
$bicepFile = Join-Path $testingDir 'bicep\main.bicep'
$envFile = Join-Path $testingDir '.env'

function Write-Step($message) {
    Write-Host "`n=== $message ===" -ForegroundColor Cyan
}

# --- Preflight -------------------------------------------------------------------------
Write-Step 'Preflight'

$account = az account show --query '{name:name, id:id, user:user.name}' -o json 2>$null | ConvertFrom-Json
if (-not $account) {
    throw 'Not logged in to Azure. Run: az login'
}
Write-Host "Subscription : $($account.name)"
Write-Host "Signed in as : $($account.user)"

# The operator needs an object ID for the ADX Admin and Grafana Admin role assignments.
# Without it the cluster deploys and then refuses the schema bootstrap, which is a confusing
# failure to debug after the fact.
$operatorObjectId = az ad signed-in-user show --query id -o tsv 2>$null
if (-not $operatorObjectId) {
    throw 'Could not resolve the signed-in user object ID. A service principal needs -operatorPrincipalId set manually.'
}
Write-Host "Operator OID : $operatorObjectId"

# MySQL public access is IP-restricted; the collector must be able to reach it from here.
Write-Host 'Detecting public IP address...'
try {
    $publicIp = (Invoke-RestMethod -Uri 'https://api.ipify.org?format=json' -TimeoutSec 15).ip
} catch {
    throw "Could not detect the public IP address: $_. Pass it manually in the parameters file."
}
if ($publicIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
    throw "Detected a non-IPv4 address ($publicIp). Flexible Server firewall rules require IPv4."
}
Write-Host "Public IP    : $publicIp"

$deployAdx = -not $SkipAdx
$deployGrafana = -not $SkipGrafana
Write-Host "Deploy ADX   : $deployAdx"
Write-Host "Deploy Grafana: $deployGrafana"

if ($deployAdx -or $deployGrafana) {
    Write-Warning 'ADX and/or Managed Grafana bill continuously while deployed. Run teardown.ps1 when finished.'
}

# --- Password --------------------------------------------------------------------------
Write-Step 'Administrator password'

# Generated rather than prompted so it is never typed, never shell-history'd, and never
# reused from another environment. It lands only in testing/.env, which is git-ignored.
$bytes = [byte[]]::new(24)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$adminPassword = ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '') + 'Aa1!'
Write-Host 'Generated a random administrator password (stored in testing/.env only).'

# --- Resource group --------------------------------------------------------------------
Write-Step "Resource group $ResourceGroup"

az group create --name $ResourceGroup --location $Location --tags project=azure-mysql-monitoring environment=test --output none
Write-Host "Ready in $Location"

# --- Deploy ----------------------------------------------------------------------------
Write-Step 'Deploying (ADX provisioning typically takes 10-15 minutes)'

$deploymentName = "mysqlmon-test-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$deployArgs = @(
    'deployment', 'group', ($WhatIf ? 'what-if' : 'create'),
    '--resource-group', $ResourceGroup,
    '--name', $deploymentName,
    '--template-file', $bicepFile,
    '--parameters',
    "namePrefix=$NamePrefix",
    "administratorLogin=$AdminLogin",
    "administratorPassword=$adminPassword",
    "databaseName=$DatabaseName",
    "clientIpAddress=$publicIp",
    "operatorPrincipalId=$operatorObjectId",
    "deployAdx=$($deployAdx.ToString().ToLower())",
    "deployGrafana=$($deployGrafana.ToString().ToLower())"
)

az @deployArgs --output none
if ($LASTEXITCODE -ne 0) { throw 'Deployment failed.' }

if ($WhatIf) {
    Write-Host "`nWhat-if complete; nothing was created." -ForegroundColor Yellow
    return
}

# --- Collect outputs -------------------------------------------------------------------
Write-Step 'Deployment outputs'

$outputs = az deployment group show --resource-group $ResourceGroup --name $deploymentName --query properties.outputs -o json | ConvertFrom-Json

$mysqlHost = $outputs.mysqlHost.value
$mysqlDb = $outputs.mysqlDatabase.value
$adxClusterUri = $outputs.adxClusterUri.value
$adxIngestUri = $outputs.adxIngestUri.value
$adxDatabase = $outputs.adxDatabase.value
$grafanaEndpoint = $outputs.grafanaEndpoint.value
$workspaceCustomerId = $outputs.workspaceCustomerId.value

Write-Host "MySQL host      : $mysqlHost"
Write-Host "MySQL version   : $($outputs.mysqlVersion.value)"
Write-Host "LA workspace ID : $workspaceCustomerId"
if ($adxClusterUri) { Write-Host "ADX cluster     : $adxClusterUri" }
if ($grafanaEndpoint) { Write-Host "Grafana         : $grafanaEndpoint" }

# --- Write .env ------------------------------------------------------------------------
Write-Step "Writing $envFile"

# RUN_ID uses the 'prod' sentinel shape for a smoke test: it is a real run identifier, so no
# query needs a special case for missing values.
$runId = "test-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

$envLines = @(
    '# Generated by testing/scripts/deploy.ps1 - DO NOT COMMIT.'
    "# Resource group: $ResourceGroup"
    "MYSQL_HOST=$mysqlHost"
    "MYSQL_USER=$AdminLogin"
    "MYSQL_PASSWORD=$adminPassword"
    "MYSQL_DB=$mysqlDb"
    'MYSQL_TIER=premium-ssd-v1'
    "RUN_ID=$runId"
    "AZURE_RESOURCE_GROUP=$ResourceGroup"
    "LOG_ANALYTICS_WORKSPACE_ID=$workspaceCustomerId"
    "ADX_CLUSTER_URI=$adxClusterUri"
    "ADX_INGEST_URI=$adxIngestUri"
    "ADX_DATABASE=$adxDatabase"
    "GRAFANA_ENDPOINT=$grafanaEndpoint"
)
$envLines | Set-Content -Path $envFile -Encoding utf8
Write-Host 'Environment written.'

Write-Step 'Next steps'
Write-Host @"
1. Load the environment:      . ./scripts/load-env.ps1
2. Apply the ADX schema:      python scripts/bootstrap_adx.py
3. Generate database load:    python scripts/workload.py --seconds 180
4. Run the collector:         python ../mysql-internal/collector/collector.py --interval 5 --sink jsonl --sink adx-streaming --out runs/`$env:RUN_ID.jsonl --max-cycles 24
5. Verify end to end:         python verify.py
6. Tear down when finished:   ./scripts/teardown.ps1 -ResourceGroup $ResourceGroup
"@ -ForegroundColor Green
