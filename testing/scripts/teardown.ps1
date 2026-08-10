<#
.SYNOPSIS
    Deletes the azure-mysql-monitoring test environment.

.DESCRIPTION
    Removes the entire resource group. The ADX cluster and the Managed Grafana workspace bill
    continuously while deployed, so this is not optional housekeeping — it is the difference
    between a few dollars and a few hundred.

    Guarded: refuses to delete a resource group that is not tagged
    project=azure-mysql-monitoring, so it cannot be pointed at production by mistake.

.EXAMPLE
    ./teardown.ps1 -ResourceGroup mysql-mon-test
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroup,

    [switch]$Force,

    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path (Split-Path -Parent $scriptDir) '.env'

Write-Host "=== Inspecting $ResourceGroup ===" -ForegroundColor Cyan

$group = az group show --name $ResourceGroup -o json 2>$null | ConvertFrom-Json
if (-not $group) {
    Write-Host "Resource group $ResourceGroup does not exist. Nothing to do." -ForegroundColor Yellow
    if (Test-Path $envFile) { Remove-Item $envFile -Force; Write-Host 'Removed stale testing/.env' }
    return
}

$projectTag = $group.tags.project
if ($projectTag -ne 'azure-mysql-monitoring' -and -not $Force) {
    throw @"
Refusing to delete $ResourceGroup.

Its 'project' tag is '$projectTag', not 'azure-mysql-monitoring'. This guard exists so the
script cannot be pointed at a production group. Re-run with -Force only if you are certain.
"@
}

$resources = az resource list --resource-group $ResourceGroup --query '[].{name:name, type:type}' -o json | ConvertFrom-Json
Write-Host "`nResources to be deleted ($($resources.Count)):"
foreach ($r in $resources) {
    Write-Host ("  {0,-55} {1}" -f $r.type, $r.name)
}

if (-not $PSCmdlet.ShouldProcess($ResourceGroup, 'Delete resource group and all resources')) {
    Write-Host 'Cancelled.' -ForegroundColor Yellow
    return
}

Write-Host "`n=== Deleting $ResourceGroup ===" -ForegroundColor Cyan

$deleteArgs = @('group', 'delete', '--name', $ResourceGroup, '--yes')
if ($NoWait) { $deleteArgs += '--no-wait' }

az @deleteArgs
if ($LASTEXITCODE -ne 0) { throw 'Deletion failed.' }

if (Test-Path $envFile) {
    Remove-Item $envFile -Force
    Write-Host 'Removed testing/.env (it held the MySQL password).'
}

if ($NoWait) {
    Write-Host "`nDeletion started in the background. Confirm with:" -ForegroundColor Green
    Write-Host "  az group exists --name $ResourceGroup"
} else {
    Write-Host "`nDeleted." -ForegroundColor Green
}
