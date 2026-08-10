// modules/adx.bicep — Azure Data Explorer cluster and database (the unified store).
//
// Dev(No SLA)_Standard_E2a_v4 is the cheapest SKU that still supports streaming ingestion,
// which is the whole point of the hot path. Two settings matter more than the SKU:
//
//   enableStreamingIngest: true   — without it the table-level streaming policy in
//                                   adx/policies/policies.kql cannot be enabled, and
//                                   "real-time" silently becomes 5-minute batching.
//   enableAutoStop:        false  — auto-stop halts an idle cluster. On a monitoring store
//                                   that is indistinguishable from an outage: ingestion
//                                   fails and dashboards flatline while the portal shows
//                                   a healthy, merely stopped, cluster.

@description('ADX cluster name; globally unique, 4-22 lowercase alphanumeric characters.')
@minLength(4)
@maxLength(22)
param clusterName string

@description('Azure region.')
param location string

@description('Database name inside the cluster.')
param databaseName string

@description('Resource tags.')
param tags object = {}

@description('Hot cache period for the database default. Raw table policies override this.')
param hotCachePeriod string = 'P7D'

@description('Soft-delete (retention) period for the database default.')
param softDeletePeriod string = 'P30D'

resource cluster 'Microsoft.Kusto/clusters@2023-08-15' = {
  name: clusterName
  location: location
  tags: tags
  sku: {
    name: 'Dev(No SLA)_Standard_E2a_v4'
    tier: 'Basic'
    capacity: 1
  }
  identity: {
    // System-assigned identity so the cluster itself can reach other Azure resources
    // without a secret. Grafana authenticates TO this cluster with its own identity.
    type: 'SystemAssigned'
  }
  properties: {
    enableStreamingIngest: true
    enableAutoStop: false
    enableDiskEncryption: false
    enablePurge: false
    publicNetworkAccess: 'Enabled'
    trustedExternalTenants: []
  }
}

resource database 'Microsoft.Kusto/clusters/databases@2023-08-15' = {
  parent: cluster
  name: databaseName
  location: location
  kind: 'ReadWrite'
  properties: {
    hotCachePeriod: hotCachePeriod
    softDeletePeriod: softDeletePeriod
  }
}

output clusterId string = cluster.id
output clusterNameOut string = cluster.name
output clusterUri string = cluster.properties.uri
output ingestUri string = cluster.properties.dataIngestionUri
output databaseNameOut string = database.name
output clusterPrincipalId string = cluster.identity.principalId
