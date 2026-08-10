// modules/adx.bicep — Azure Data Explorer cluster and database (the unified store).
//
// The SKU is a parameter because Dev SKU availability is region-specific: koreacentral offers
// only Dev(No SLA)_Standard_D11_v2, while other regions carry the newer E2a_v4. Deploying an
// unavailable SKU fails at the cluster with "The sku X is not supported in <region>", so check
// `az kusto cluster list-sku` for the target region before changing the default.
//
// Two settings matter more than the SKU:
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

@description('Dev-tier SKU name. Availability is region-specific; verify with `az kusto cluster list-sku`.')
param skuName string = 'Dev(No SLA)_Standard_D11_v2'

resource cluster 'Microsoft.Kusto/clusters@2023-08-15' = {
  name: clusterName
  location: location
  tags: tags
  sku: {
    name: skuName
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
