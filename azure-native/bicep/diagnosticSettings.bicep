// diagnosticSettings.bicep — routes Flexible Server logs and metrics into Log Analytics.
//
// Flexible Server exposes exactly TWO resource log categories:
//
//   MySqlSlowLogs   — queries slower than long_query_time
//   MySqlAuditLogs  — connection/query audit events
//
// Both land in the AzureDiagnostics table. There is NO error-log category; that gap is what
// mysql-internal/ fills by reading performance_schema.error_log directly. testing/verify.py
// asserts the absence so this stays a proven limitation rather than a remembered one.
//
// Enabling a category here is only half the job. Both logs are also gated by server
// parameters, and a diagnostic setting pointed at a server that never emits produces an empty
// table that is indistinguishable from a broken pipeline:
//
//   slow logs   slow_query_log=ON, and long_query_time low enough to actually match
//   audit logs  audit_log_enabled=ON, and audit_log_events selecting the event classes
//
// Server parameters are deliberately NOT set here — changing audit_log_events on a production
// server can multiply ingestion cost. See azure-native/README.md for the az commands.

@description('Name of the existing MySQL Flexible Server to collect from.')
param mysqlServerName string

@description('Resource ID of the Log Analytics workspace receiving the telemetry.')
param workspaceId string

@description('Name of the diagnostic setting.')
param settingName string = 'mysql-to-law'

@description('Send MySqlSlowLogs. Requires slow_query_log=ON on the server.')
param enableSlowLogs bool = true

@description('Send MySqlAuditLogs. Requires audit_log_enabled=ON and a non-empty audit_log_events.')
param enableAuditLogs bool = true

@description('Send platform metrics (AllMetrics) to the AzureMetrics table.')
param enableMetrics bool = true

resource mysqlServer 'Microsoft.DBforMySQL/flexibleServers@2023-12-30' existing = {
  name: mysqlServerName
}

resource diagnosticSetting 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: settingName
  scope: mysqlServer
  properties: {
    workspaceId: workspaceId
    // Categories are listed even when disabled. Omitting a category leaves its previous state
    // untouched on an existing setting, so an explicit `enabled: false` is the only way to
    // actually turn one off.
    logs: [
      {
        category: 'MySqlSlowLogs'
        enabled: enableSlowLogs
      }
      {
        category: 'MySqlAuditLogs'
        enabled: enableAuditLogs
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: enableMetrics
      }
    ]
  }
}

output diagnosticSettingId string = diagnosticSetting.id
