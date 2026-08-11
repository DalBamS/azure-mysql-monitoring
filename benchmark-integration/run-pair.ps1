<#
.SYNOPSIS
    Runs repeated, concurrent workloads against a deployed v1/v2 pair.

.DESCRIPTION
    Loads MySQL credentials from .env.benchmark and the existing ADX endpoints from testing/.env.
    Each repetition starts one multi-Target collector, then starts identical workloads against both
    servers at the same time. Raw output and generated reports stay under runs/, which is ignored.
#>
[CmdletBinding()]
param(
    [int]$Repetitions = 3,
    [int]$Seconds = 120,
    [int]$Threads = 8,
    [int]$Seed = 42,
    [int]$CollectorCycles = 36,
    [string]$BatchId = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $root
$benchmarkEnv = Join-Path $root '.env.benchmark'
$monitoringEnv = Join-Path $repoRoot 'testing\.env'
$collector = Join-Path $repoRoot 'mysql-internal\collector\collector.py'
$workload = Join-Path $repoRoot 'testing\scripts\workload.py'
$reporter = Join-Path $root 'performance_report.py'
$uploader = Join-Path $root 'upload_jsonl.py'

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

function Assert-ProcessSucceeded(
    [System.Diagnostics.Process]$Process,
    [string]$Label,
    [string]$ErrorLog
) {
    $Process.WaitForExit()
    if ($Process.ExitCode -ne 0) {
        $tail = Get-Content -LiteralPath $ErrorLog -Tail 30 -ErrorAction SilentlyContinue
        throw "$Label failed with exit code $($Process.ExitCode).`n$($tail -join "`n")"
    }
}

if ($Repetitions -lt 1 -or $Seconds -lt 30 -or $Threads -lt 1 -or $CollectorCycles -lt 1) {
    throw 'Repetitions, Threads and CollectorCycles must be positive; Seconds must be at least 30.'
}

# Load ADX first, then benchmark credentials. The second file intentionally replaces MySQL values.
Import-EnvFile $monitoringEnv
Import-EnvFile $benchmarkEnv

$required = @(
    'ADX_CLUSTER_URI', 'ADX_INGEST_URI', 'ADX_DATABASE', 'GRAFANA_ENDPOINT',
    'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DB',
    'BASELINE_MYSQL_HOST', 'BASELINE_TARGET_ID', 'BASELINE_TIER',
    'CANDIDATE_MYSQL_HOST', 'CANDIDATE_TARGET_ID', 'CANDIDATE_TIER',
    'AZURE_SUBSCRIPTION_ID', 'AZURE_RESOURCE_GROUP'
)
foreach ($name in $required) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Required environment variable $name is missing."
    }
}

$batch = if ([string]::IsNullOrWhiteSpace($BatchId)) {
    "storage-$(Get-Date -Format 'yyyyMMdd-HHmmss')-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
} else {
    if ($BatchId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$') {
        throw 'BatchId must be 1-96 characters and contain only letters, digits, dot, underscore or hyphen.'
    }
    $BatchId
}
$batchDir = Join-Path $root "runs\$batch"
if (Test-Path -LiteralPath $batchDir) {
    throw "Batch directory already exists; choose a unique BatchId: $batchDir"
}
New-Item -ItemType Directory -Path $batchDir | Out-Null

for ($rep = 1; $rep -le $Repetitions; $rep++) {
    $baselineRun = "ssdv1-$batch-r$rep"
    $candidateRun = "ssdv2-$batch-r$rep"
    $repDir = Join-Path $batchDir "r$rep"
    New-Item -ItemType Directory -Path $repDir | Out-Null

    $configPath = Join-Path $repDir 'collector.yaml'
    $resourceBase = (
        "/subscriptions/$env:AZURE_SUBSCRIPTION_ID/resourceGroups/$env:AZURE_RESOURCE_GROUP" +
        '/providers/Microsoft.DBforMySQL/flexibleServers'
    )
    @"
version: 1
profiles:
  benchmark:
    allow_high_cardinality: true
    groups:
      collector-health:
        interval: 5s
      global-status:
        interval: 5s
      innodb-metrics:
        interval: 10s
      error-log:
        interval: 5s
      file-io:
        interval: 30s
        top_k: 100
      statement-digests:
        interval: 30s
        top_k: 100
targets:
  - id: $env:BASELINE_TARGET_ID
    host: $env:BASELINE_MYSQL_HOST
    port: 3306
    database: $env:MYSQL_DB
    tier: $env:BASELINE_TIER
    profile: benchmark
    azure_resource_id: $resourceBase/$env:BASELINE_TARGET_ID
    run_id:
      env: BASELINE_RUN_ID
    credentials:
      username:
        env: MYSQL_USER
      password:
        env: MYSQL_PASSWORD
  - id: $env:CANDIDATE_TARGET_ID
    host: $env:CANDIDATE_MYSQL_HOST
    port: 3306
    database: $env:MYSQL_DB
    tier: $env:CANDIDATE_TIER
    profile: benchmark
    azure_resource_id: $resourceBase/$env:CANDIDATE_TARGET_ID
    run_id:
      env: CANDIDATE_RUN_ID
    credentials:
      username:
        env: MYSQL_USER
      password:
        env: MYSQL_PASSWORD
"@ | Set-Content -LiteralPath $configPath -Encoding utf8

    $env:BASELINE_RUN_ID = $baselineRun
    $env:CANDIDATE_RUN_ID = $candidateRun
    $started = [DateTime]::UtcNow.AddSeconds(-5).ToString('o')

    Write-Host "`n=== Repetition $rep/$Repetitions ===" -ForegroundColor Cyan
    Write-Host "Baseline : $baselineRun"
    Write-Host "Candidate: $candidateRun"

    $collectorOut = Join-Path $repDir 'collector.stdout.log'
    $collectorErr = Join-Path $repDir 'collector.stderr.log'
    $collectorJsonl = Join-Path $repDir 'telemetry.jsonl'
    $collectorProcess = Start-Process python -PassThru -NoNewWindow `
        -ArgumentList @(
            $collector, '--config', $configPath,
            '--sink', 'jsonl',
            '--out', $collectorJsonl,
            '--max-cycles', $CollectorCycles,
            '--cursor-dir', (Join-Path $repDir 'cursors')
        ) `
        -RedirectStandardOutput $collectorOut `
        -RedirectStandardError $collectorErr

    Start-Sleep -Seconds 10
    if ($collectorProcess.HasExited) {
        Assert-ProcessSucceeded $collectorProcess 'collector startup' $collectorErr
        throw 'Collector exited before workloads started.'
    }

    $env:MYSQL_HOST = $env:BASELINE_MYSQL_HOST
    $env:MYSQL_TIER = $env:BASELINE_TIER
    $env:RUN_ID = $baselineRun
    $baselineOut = Join-Path $repDir 'baseline.stdout.log'
    $baselineErr = Join-Path $repDir 'baseline.stderr.log'
    $baselineProcess = Start-Process python -PassThru -NoNewWindow `
        -ArgumentList @(
            $workload, '--seconds', $Seconds, '--threads', $Threads, '--seed', ($Seed + $rep)
        ) `
        -RedirectStandardOutput $baselineOut `
        -RedirectStandardError $baselineErr

    $env:MYSQL_HOST = $env:CANDIDATE_MYSQL_HOST
    $env:MYSQL_TIER = $env:CANDIDATE_TIER
    $env:RUN_ID = $candidateRun
    $candidateOut = Join-Path $repDir 'candidate.stdout.log'
    $candidateErr = Join-Path $repDir 'candidate.stderr.log'
    $candidateProcess = Start-Process python -PassThru -NoNewWindow `
        -ArgumentList @(
            $workload, '--seconds', $Seconds, '--threads', $Threads, '--seed', ($Seed + $rep)
        ) `
        -RedirectStandardOutput $candidateOut `
        -RedirectStandardError $candidateErr

    Assert-ProcessSucceeded $baselineProcess 'baseline workload' $baselineErr
    Assert-ProcessSucceeded $candidateProcess 'candidate workload' $candidateErr
    Assert-ProcessSucceeded $collectorProcess 'collector' $collectorErr

    $ended = [DateTime]::UtcNow.AddSeconds(5).ToString('o')
    & python $uploader --input $collectorJsonl
    if ($LASTEXITCODE -ne 0) {
        throw "ADX bulk upload failed for repetition $rep."
    }
    Start-Sleep -Seconds 10

    $reportPath = Join-Path $repDir 'performance-report.md'
    & python $reporter `
        --baseline-run $baselineRun `
        --baseline-target $env:BASELINE_TARGET_ID `
        --candidate-run $candidateRun `
        --candidate-target $env:CANDIDATE_TARGET_ID `
        --from-utc $started `
        --to-utc $ended `
        --output $reportPath
    if ($LASTEXITCODE -ne 0) {
        throw "Report generation failed for repetition $rep."
    }
}

Write-Host "`nBenchmark complete: $batchDir" -ForegroundColor Green
