// modules/monitoring.bicep — Layer 1: Log Analytics workspace and diagnostic settings.
//
// Flexible Server exposes exactly TWO resource log categories, MySqlSlowLogs and
// MySqlAuditLogs, both landing in the AzureDiagnostics table. There is no error-log
// category — that gap is what mysql-internal/ fills via performance_schema.error_log, and
// the verification script asserts the absence explicitly so the limitation stays proven
// rather than remembered.

@description('Log Analytics workspace name.')
param workspaceName string

@description('Azure region.')
param location string

@description('Resource ID of the MySQL Flexible Server to collect from.')
param mysqlServerId string

@description('Resource tags.')
param tags object = {}

@description('Workspace retention in days. 30 is the minimum billable-free period.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

@description('Daily ingestion cap in GB. Guards against a runaway test bill.')
param dailyQuotaGb int = 1

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    workspaceCapping: {
      dailyQuotaGb: dailyQuotaGb
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource mysqlServer 'Microsoft.DBforMySQL/flexibleServers@2023-12-30' existing = {
  name: last(split(mysqlServerId, '/'))
}

resource diagnosticSetting 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'mysql-to-law'
  scope: mysqlServer
  properties: {
    workspaceId: workspace.id
    logs: [
      {
        category: 'MySqlSlowLogs'
        enabled: true
      }
      {
        category: 'MySqlAuditLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output workspaceId string = workspace.id
output workspaceCustomerId string = workspace.properties.customerId
output workspaceNameOut string = workspace.name
