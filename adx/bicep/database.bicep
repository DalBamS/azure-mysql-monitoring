// database.bicep — the database holding MysqlMetrics and MysqlEvents.
//
// These are DATABASE-level defaults only. The per-table policies that actually shape cost and
// latency live in adx/policies/policies.kql and are applied by
// testing/scripts/bootstrap_adx.py, because table policies cannot be expressed in ARM.
//
// Keeping table policies out of Bicep is deliberate: every telemetry object is pinned to the
// repository's 90-day lifecycle in adx/policies/policies.kql, next to the objects it governs.

@description('Name of the parent ADX cluster.')
param clusterName string

@description('Database name.')
param databaseName string = 'mysqlmonitoring'

@description('Azure region. Must match the cluster.')
param location string = resourceGroup().location

@description('''
Default soft-delete (retention) period.

This is only a fallback for objects with no table policy. Every telemetry table and materialized
view also receives an explicit 90-day policy from adx/policies/policies.kql.
''')
param softDeletePeriod string = 'P90D'

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
