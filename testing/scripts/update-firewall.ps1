<#
.SYNOPSIS
    Repoints the MySQL firewall rule at this machine's current public IP address.

.DESCRIPTION
    The test server is IP-restricted, and consumer connections get a new public IP whenever the
    ISP feels like it. When that happens the symptom is a bare TCP timeout:

        2003: Can't connect to MySQL server on '...' (timed out)

    That looks identical to a server outage, a wrong hostname or a dead network, so it costs
    real time to diagnose. Run this script when a connection that used to work starts timing
    out; it is a no-op when the address has not changed.

.EXAMPLE
    ./update-firewall.ps1 -ResourceGroup mysql-mon-test
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroup,

    [string]$RuleName = 'allow-test-client',

    [string]$ServerName
)

$ErrorActionPreference = 'Stop'

if (-not $ServerName) {
    $ServerName = az mysql flexible-server list --resource-group $ResourceGroup --query '[0].name' -o tsv
    if (-not $ServerName) { throw "No MySQL Flexible Server found in $ResourceGroup." }
}

try {
    $publicIp = (Invoke-RestMethod -Uri 'https://api.ipify.org?format=json' -TimeoutSec 15).ip
} catch {
    throw "Could not detect the public IP address: $_"
}
if ($publicIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
    throw "Detected a non-IPv4 address ($publicIp). Flexible Server firewall rules require IPv4."
}

$current = az mysql flexible-server firewall-rule show `
    --resource-group $ResourceGroup --name $ServerName --rule-name $RuleName `
    --query startIpAddress -o tsv 2>$null

if ($current -eq $publicIp) {
    Write-Host "Firewall rule '$RuleName' already allows $publicIp; nothing to do." -ForegroundColor Green
    return
}

Write-Host "Public IP changed: $current -> $publicIp" -ForegroundColor Yellow

az mysql flexible-server firewall-rule update `
    --resource-group $ResourceGroup --name $ServerName --rule-name $RuleName `
    --start-ip-address $publicIp --end-ip-address $publicIp --output none
if ($LASTEXITCODE -ne 0) { throw 'Failed to update the firewall rule.' }

Write-Host "Firewall rule '$RuleName' now allows $publicIp." -ForegroundColor Green
