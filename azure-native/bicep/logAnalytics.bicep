// logAnalytics.bicep — the Log Analytics workspace that receives Layer 1 telemetry.
//
// Deploying a workspace is optional: most organisations already have one, and splitting MySQL
// telemetry into a second workspace makes cross-service correlation queries impossible without
// a cross-workspace join. Set createWorkspace=false and pass existingWorkspaceId to reuse one.

@description('Workspace name. Ignored when createWorkspace is false.')
param workspaceName string

@description('Azure region.')
param location string = resourceGroup().location

@description('Resource tags.')
param tags object = {}

@description('''
Retention in days.

Slow-log volume is driven by long_query_time, not by traffic, so this is usually the cheaper
knob to adjust. Raw retention beyond 90 days is rarely worth it here: adx/ keeps the
long-horizon rollups, and Layer 1 is the supplementary source.
''')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

@description('''
Daily ingestion cap in GB. 0 disables the cap.

A cap is a blunt instrument — once hit, ingestion stops for the rest of the day and the gap
looks exactly like an outage. Prefer tuning long_query_time and audit_log_events first, and
treat this as a last-resort guard against a runaway bill.
''')
param dailyQuotaGb int = 0

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    workspaceCapping: dailyQuotaGb > 0 ? { dailyQuotaGb: dailyQuotaGb } : null
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

output workspaceId string = workspace.id
output workspaceCustomerId string = workspace.properties.customerId
output workspaceName string = workspace.name
