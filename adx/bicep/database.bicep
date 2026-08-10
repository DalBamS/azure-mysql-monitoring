// database.bicep — the database holding MysqlMetrics and MysqlEvents.
//
// These are DATABASE-level defaults only. The per-table policies that actually shape cost and
// latency live in adx/policies/policies.kql and are applied by
// testing/scripts/bootstrap_adx.py, because table policies cannot be expressed in ARM.
//
// Keeping them out of Bicep is deliberate: the retention split (raw 30d, rollups 395d) is the
// mechanism that stops growing log volume from growing cost linearly, and it belongs next to
// the table definitions it applies to rather than in a template that never mentions them.

@description('Name of the parent ADX cluster.')
param clusterName string

@description('Database name.')
param databaseName string = 'mysqlmonitoring'

@description('Azure region. Must match the cluster.')
param location string = resourceGroup().location

@description('''
Default soft-delete (retention) period.

This is only a fallback for objects with no table policy. The real retention is set per table:
raw data expires at 30 days while materialized rollups are kept for 395.
''')
param softDeletePeriod string = 'P30D'

@description('Default hot cache period. Table policies override this.')
param hotCachePeriod string = 'P7D'

resource cluster 'Microsoft.Kusto/clusters@2023-08-15' existing = {
  name: clusterName
}

resource database 'Microsoft.Kusto/clusters/databases@2023-08-15' = {
  parent: cluster
  name: databaseName
  location: location
  kind: 'ReadWrite'
  properties: {
    softDeletePeriod: softDeletePeriod
    hotCachePeriod: hotCachePeriod
  }
}

output databaseName string = database.name
output databaseId string = database.id
