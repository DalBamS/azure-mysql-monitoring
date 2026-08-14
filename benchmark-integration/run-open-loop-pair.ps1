<#
.SYNOPSIS
    Runs a fixed-rate, read-heavy benchmark against the deployed v1/v2 pair.

.DESCRIPTION
    Temporarily reduces both buffer pools, prepares identical datasets larger than the buffer
    pool, delegates repeated collection and workload execution to run-pair.ps1, then removes the
    datasets and restores the original server configuration even when the benchmark fails.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MonitoringSubscription,
    [int]$Repetitions = 3,
    [int]$Seconds = 120,
    [int]$Threads = 16,
    [int]$OfferedRate = 500,
    [ValidateRange(0, 100)]
    [int]$ReadPercent = 80,
    [int]$DatasetMiB = 2048,
    [long]$BufferPoolBytes = 1073741824,
    [int]$Seed = 42,
    [int]$CollectorCycles = 36,
    [string]$BatchId = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $root
$benchmarkEnv = Join-Path $root '.env.benchmark'
$monitoringEnv = Join-Path $repoRoot 'testing\.env'
$workload = Join-Path $root 'open_loop_workload.py'
$runner = Join-Path $root 'run-pair.ps1'

function Import-EnvFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required environment file does not exist: $Path"
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $split = $trimmed.IndexOf('=')
        if ($split -lt 1) { continue }
        [Environment]::SetEnvironmentVariable(
            $trimmed.Substring(0, $split),
            $trimmed.Substring($split + 1)
        )
    }
}

function Set-TargetEnvironment([ValidateSet('baseline', 'candidate')] [string]$Target) {
    if ($Target -eq 'baseline') {
        $env:MYSQL_HOST = $env:BASELINE_MYSQL_HOST
        $env:MYSQL_TIER = $env:BASELINE_TIER
        $env:RUN_ID = 'prepare-open-loop-baseline'
    } else {
        $env:MYSQL_HOST = $env:CANDIDATE_MYSQL_HOST
        $env:MYSQL_TIER = $env:CANDIDATE_TIER
        $env:RUN_ID = 'prepare-open-loop-candidate'
    }
}

function Set-BufferPoolValue([hashtable]$Server, [string]$Value) {
    @{ properties = @{ value = $Value } } |
        ConvertTo-Json -Depth 3 |
        Set-Content -LiteralPath $parameterFile -Encoding utf8
    $uri = (
        "https://management.azure.com/subscriptions/$env:AZURE_SUBSCRIPTION_ID/" +
        "resourceGroups/$env:AZURE_RESOURCE_GROUP/providers/Microsoft.DBforMySQL/" +
        "flexibleServers/$($Server.Name)/configurations/innodb_buffer_pool_size" +
        "?api-version=$($Server.Api)"
    )
    az rest --method put --uri $uri --body "@$parameterFile" `
        --headers 'Content-Type=application/json' --output none
    return $LASTEXITCODE -eq 0
}

function Get-BufferPoolValue([hashtable]$Server) {
    $uri = (
        "https://management.azure.com/subscriptions/$env:AZURE_SUBSCRIPTION_ID/" +
        "resourceGroups/$env:AZURE_RESOURCE_GROUP/providers/Microsoft.DBforMySQL/" +
        "flexibleServers/$($Server.Name)/configurations/innodb_buffer_pool_size" +
        "?api-version=$($Server.Api)"
    )
    $value = az rest --method get --uri $uri --query properties.value -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $value) {
        throw "Cannot read innodb_buffer_pool_size from $($Server.Name)."
    }
    return $value
}

function Get-ServerBaseUri([hashtable]$Server) {
    return (
        "https://management.azure.com/subscriptions/$env:AZURE_SUBSCRIPTION_ID/" +
        "resourceGroups/$env:AZURE_RESOURCE_GROUP/providers/Microsoft.DBforMySQL/" +
        "flexibleServers/$($Server.Name)"
    )
}

function Get-ServerState([hashtable]$Server) {
    $baseUri = Get-ServerBaseUri $Server
    $state = az rest --method get `
        --uri "${baseUri}?api-version=$($Server.Api)" `
        --query properties.state -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $state) {
        throw "Cannot read state for $($Server.Name)."
    }
    return $state
}

function Wait-ServerState(
    [hashtable]$Server,
    [string]$ExpectedState,
    [int]$TimeoutSeconds = 900
) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $state = Get-ServerState $Server
        Write-Host "$($Server.Name): $state (waiting for $ExpectedState)"
        if ($state -eq $ExpectedState) {
            return $true
        }
        Start-Sleep -Seconds 10
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Ensure-ServerReady([hashtable]$Server) {
    $state = Get-ServerState $Server
    if ($state -eq 'Ready') {
        return $true
    }
    if ($state -notin @('Stopped', 'Stopping')) {
        return Wait-ServerState $Server 'Ready'
    }
    if ($state -eq 'Stopping') {
        if (-not (Wait-ServerState $Server 'Stopped')) {
            return $false
        }
    }
    $baseUri = Get-ServerBaseUri $Server
    az rest --method post `
        --uri "$baseUri/start?api-version=$($Server.Api)" `
        --output none
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    return Wait-ServerState $Server 'Ready'
}

function Restart-Server([hashtable]$Server) {
    $state = Get-ServerState $Server
    if ($state -ne 'Stopped') {
        $baseUri = Get-ServerBaseUri $Server
        az rest --method post `
            --uri "$baseUri/stop?api-version=$($Server.Api)" `
            --output none
        if ($LASTEXITCODE -ne 0 -or -not (Wait-ServerState $Server 'Stopped')) {
            return $false
        }
    }
    return Ensure-ServerReady $Server
}

function Test-AdxEndpoint {
    $metadataUri = "$($env:ADX_CLUSTER_URI.TrimEnd('/'))/v1/rest/auth/metadata"
    try {
        $response = Invoke-WebRequest `
            -Uri $metadataUri `
            -Method Get `
            -TimeoutSec 30 `
            -UseBasicParsing
    } catch {
        throw "ADX endpoint is unavailable: $metadataUri. $($_.Exception.Message)"
    }
    if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
        throw "ADX endpoint returned HTTP $($response.StatusCode): $metadataUri"
    }
}

if ($DatasetMiB -lt 128 -or $BufferPoolBytes -lt 134217728) {
    throw 'DatasetMiB must be at least 128 and BufferPoolBytes must be at least 128 MiB.'
}

Import-EnvFile $monitoringEnv
Import-EnvFile $benchmarkEnv
$required = @(
    'ADX_CLUSTER_URI',
    'AZURE_SUBSCRIPTION_ID', 'AZURE_RESOURCE_GROUP',
    'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DB',
    'BASELINE_MYSQL_HOST', 'BASELINE_TARGET_ID', 'BASELINE_TIER',
    'CANDIDATE_MYSQL_HOST', 'CANDIDATE_TARGET_ID', 'CANDIDATE_TIER'
)
foreach ($name in $required) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Required environment variable $name is missing."
    }
}

$originalSubscription = az account show --query id -o tsv
if (-not $originalSubscription) {
    throw 'Azure CLI is not authenticated.'
}

$servers = @(
    @{ Target = 'baseline'; Name = $env:BASELINE_TARGET_ID; Api = '2023-12-30' },
    @{ Target = 'candidate'; Name = $env:CANDIDATE_TARGET_ID; Api = '2025-12-01-preview' }
)
$parameterFile = [System.IO.Path]::GetTempFileName()
$originalBufferPools = @{}
$changedBufferPools = @{}
$cleanupErrors = [System.Collections.Generic.List[string]]::new()
$runError = $null

try {
    az account set --subscription $MonitoringSubscription
    if ($LASTEXITCODE -ne 0) {
        throw 'Cannot select the monitoring subscription for ADX preflight.'
    }
    Test-AdxEndpoint

    az account set --subscription $env:AZURE_SUBSCRIPTION_ID
    if ($LASTEXITCODE -ne 0) {
        throw 'Cannot select the benchmark subscription.'
    }

    foreach ($server in $servers) {
        if (-not (Ensure-ServerReady $server)) {
            throw "Server is not ready: $($server.Name)."
        }
        $originalBufferPools[$server.Name] = Get-BufferPoolValue $server
        if ([long]$originalBufferPools[$server.Name] -ne $BufferPoolBytes) {
            if (-not (Set-BufferPoolValue $server ([string]$BufferPoolBytes))) {
                throw "Cannot set innodb_buffer_pool_size on $($server.Name)."
            }
            $changedBufferPools[$server.Name] = $true
        }
    }

    foreach ($server in $servers) {
        Set-TargetEnvironment $server.Target
        & python $workload wait-buffer-pool `
            --expected-bytes $BufferPoolBytes `
            --timeout-seconds 600
        if ($LASTEXITCODE -ne 0) {
            throw "Buffer pool resize failed for $($server.Name)."
        }
        if (
            $changedBufferPools.ContainsKey($server.Name) -and
            -not (Restart-Server $server)
        ) {
            throw "Server recycle failed after buffer pool resize: $($server.Name)."
        }
        if ($changedBufferPools.ContainsKey($server.Name)) {
            & python $workload wait-buffer-pool `
                --expected-bytes $BufferPoolBytes `
                --timeout-seconds 600
            if ($LASTEXITCODE -ne 0) {
                throw "Buffer pool verification failed after recycle: $($server.Name)."
            }
        }
    }

    foreach ($server in $servers) {
        Set-TargetEnvironment $server.Target
        & python $workload prepare `
            --dataset-mib $DatasetMiB `
            --seed $Seed `
            --result (Join-Path $root "runs\$($server.Target)-dataset.json")
        if ($LASTEXITCODE -ne 0) {
            throw "Dataset preparation failed for $($server.Name)."
        }
    }

    az account set --subscription $MonitoringSubscription
    if ($LASTEXITCODE -ne 0) {
        throw 'Cannot select the monitoring subscription.'
    }
    & $runner `
        -Repetitions $Repetitions `
        -Seconds $Seconds `
        -Threads $Threads `
        -Seed $Seed `
        -CollectorCycles $CollectorCycles `
        -BatchId $BatchId `
        -WorkloadMode open-loop `
        -OfferedRate $OfferedRate `
        -ReadPercent $ReadPercent
    if ($LASTEXITCODE -ne 0) {
        throw 'Open-loop pair benchmark failed.'
    }
} catch {
    $runError = $_
} finally {
    az account set --subscription $env:AZURE_SUBSCRIPTION_ID
    if ($LASTEXITCODE -ne 0) {
        $cleanupErrors.Add('Cannot select the benchmark subscription for cleanup.')
    } else {
        foreach ($server in $servers) {
            try {
                if (-not (Ensure-ServerReady $server)) {
                    throw "Server recovery failed for $($server.Name)."
                }
                Set-TargetEnvironment $server.Target
                & python $workload cleanup
                if ($LASTEXITCODE -ne 0) {
                    throw "Dataset cleanup failed for $($server.Name)."
                }
                if ($changedBufferPools.ContainsKey($server.Name)) {
                    if (-not (Set-BufferPoolValue $server $originalBufferPools[$server.Name])) {
                        throw "Buffer pool restore failed for $($server.Name)."
                    }
                    & python $workload wait-buffer-pool `
                        --expected-bytes $originalBufferPools[$server.Name] `
                        --timeout-seconds 600
                    if ($LASTEXITCODE -ne 0) {
                        throw "Buffer pool runtime restore failed for $($server.Name)."
                    }
                    if (-not (Restart-Server $server)) {
                        throw "Server recycle failed after restore: $($server.Name)."
                    }
                    & python $workload wait-buffer-pool `
                        --expected-bytes $originalBufferPools[$server.Name] `
                        --timeout-seconds 600
                    if ($LASTEXITCODE -ne 0) {
                        throw "Buffer pool verification failed after restore: $($server.Name)."
                    }
                }
            } catch {
                $cleanupErrors.Add([string]$_)
            }
        }
    }
    az account set --subscription $originalSubscription
    if (Test-Path -LiteralPath $parameterFile) {
        Remove-Item -LiteralPath $parameterFile -Force
    }
}

if ($runError) {
    if ($cleanupErrors.Count) {
        throw "$runError`nCleanup failures:`n$($cleanupErrors -join [Environment]::NewLine)"
    }
    throw $runError
}
if ($cleanupErrors.Count) {
    throw ($cleanupErrors -join [Environment]::NewLine)
}
