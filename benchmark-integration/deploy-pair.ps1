<#
.SYNOPSIS
    Deploys matched MySQL 8.4 Premium SSD v1/v2 benchmark targets.

.DESCRIPTION
    The subscription is mandatory to prevent accidental deployment into the current monitoring
    subscription. A random password is written only to .env.benchmark, which is git-ignored.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Subscription,

    [string]$ResourceGroup = 'mysql-storage-benchmark',
    [string]$Location = 'koreacentral',
    [string]$NamePrefix = 'mysqlbm',
    [string]$AdministratorLogin = 'mysqladmin',
    [string]$DatabaseName = 'benchmark',
    [string]$BaselineSkuName = 'Standard_E8ds_v5',
    [string]$CandidateSkuName = 'Standard_E8ds_v6',
    [int]$StorageSizeGB = 64,
    [int]$CandidateIops = 5000,
    [string[]]$AdditionalClientIps = @(),
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bicepFile = Join-Path $root 'bicep\main.bicep'
$envFile = Join-Path $root '.env.benchmark'
$originalSubscription = az account show --query id -o tsv
$parameterFile = $null

if (-not $originalSubscription) {
    throw 'Azure CLI is not authenticated.'
}

try {
    az account set --subscription $Subscription
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot select subscription '$Subscription'."
    }

    $account = az account show --query '{name:name,id:id}' -o json | ConvertFrom-Json
    Write-Host "Subscription : $($account.name) [$($account.id)]"
    Write-Host "Region       : $Location"

    $publicIp = (Invoke-RestMethod -Uri 'https://api.ipify.org?format=json' -TimeoutSec 15).ip
    $allowedIps = @(@($publicIp) + @($AdditionalClientIps) |
        Where-Object { $_ } |
        Sort-Object -Unique)
    foreach ($ip in $allowedIps) {
        if ($ip -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
            throw "Invalid IPv4 address: $ip"
        }
    }

    $bytes = [byte[]]::new(24)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $password = ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '') + 'Aa1!'

    az group create --name $ResourceGroup --location $Location `
        --tags project=azure-mysql-monitoring environment=benchmark `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw 'Resource group creation failed.'
    }

    $deploymentName = "mysql-storage-pair-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    $operation = $ValidateOnly ? 'validate' : 'create'
    $parameterFile = [System.IO.Path]::GetTempFileName()
    @{
        '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
        contentVersion = '1.0.0.0'
        parameters = @{
            namePrefix = @{ value = $NamePrefix }
            administratorLogin = @{ value = $AdministratorLogin }
            administratorPassword = @{ value = $password }
            databaseName = @{ value = $DatabaseName }
            baselineSkuName = @{ value = $BaselineSkuName }
            candidateSkuName = @{ value = $CandidateSkuName }
            storageSizeGB = @{ value = $StorageSizeGB }
            candidateIops = @{ value = $CandidateIops }
            allowedClientIps = @{ value = $allowedIps }
        }
    } | ConvertTo-Json -Depth 6 | Set-Content -Path $parameterFile -Encoding utf8

    $result = az deployment group $operation `
        --resource-group $ResourceGroup `
        --name $deploymentName `
        --template-file $bicepFile `
        --parameters "@$parameterFile" `
        --output json
    if ($LASTEXITCODE -ne 0) {
        throw "ARM deployment $operation failed."
    }

    if ($ValidateOnly) {
        Write-Host 'ARM validation succeeded; no MySQL servers were created.' -ForegroundColor Green
        return
    }

    $outputs = ($result | ConvertFrom-Json).properties.outputs
    $apiVersion = '2025-12-01-preview'
    $baselineResource = az rest --method get --uri (
        "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/" +
        "$ResourceGroup/providers/Microsoft.DBforMySQL/flexibleServers/" +
        "$($outputs.baselineServerName.value)?api-version=$apiVersion"
    ) -o json | ConvertFrom-Json
    $candidateResource = az rest --method get --uri (
        "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/" +
        "$ResourceGroup/providers/Microsoft.DBforMySQL/flexibleServers/" +
        "$($outputs.candidateServerName.value)?api-version=$apiVersion"
    ) -o json | ConvertFrom-Json
    $baselineStorageSku = $baselineResource.properties.storage.storageSku
    $candidateStorageSku = $candidateResource.properties.storage.storageSku
    if ($baselineStorageSku -ne 'Premium_LRS' -or $candidateStorageSku -ne 'PremiumV2_LRS') {
        throw (
            'Storage tier verification failed: ' +
            "baseline=$baselineStorageSku, candidate=$candidateStorageSku. " +
            'Do not run a benchmark against misclassified Targets.'
        )
    }
    @(
        '# Generated by benchmark-integration/deploy-pair.ps1 - DO NOT COMMIT.'
        "AZURE_SUBSCRIPTION_ID=$($account.id)"
        "AZURE_RESOURCE_GROUP=$ResourceGroup"
        "MYSQL_USER=$AdministratorLogin"
        "MYSQL_PASSWORD=$password"
        "MYSQL_DB=$($outputs.database.value)"
        "BASELINE_MYSQL_HOST=$($outputs.baselineHost.value)"
        "BASELINE_TARGET_ID=$($outputs.baselineServerName.value)"
        'BASELINE_TIER=premium-ssd-v1'
        "CANDIDATE_MYSQL_HOST=$($outputs.candidateHost.value)"
        "CANDIDATE_TARGET_ID=$($outputs.candidateServerName.value)"
        'CANDIDATE_TIER=premium-ssd-v2'
        "BASELINE_SKU=$($outputs.baselineSku.value)"
        "CANDIDATE_SKU=$($outputs.candidateSku.value)"
        "BENCHMARK_STORAGE_GIB=$($outputs.storageGiB.value)"
        "CANDIDATE_IOPS=$($outputs.candidateIops.value)"
    ) | Set-Content -Path $envFile -Encoding utf8

    Write-Host "Baseline  : $($outputs.baselineHost.value) [$baselineStorageSku]"
    Write-Host "Candidate : $($outputs.candidateHost.value) [$candidateStorageSku]"
    Write-Host "Secrets   : $envFile" -ForegroundColor Green
} finally {
    if ($parameterFile -and (Test-Path -LiteralPath $parameterFile)) {
        Remove-Item -LiteralPath $parameterFile -Force
    }
    az account set --subscription $originalSubscription
}
