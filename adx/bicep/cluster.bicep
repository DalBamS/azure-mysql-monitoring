// cluster.bicep — the ADX cluster that stores both metrics and log events.
//
// Two settings matter more than the SKU:
//
//   enableStreamingIngest: true   — without it the table-level streaming policy in
//                                   adx/policies/policies.kql cannot be enabled, and the
//                                   "real-time" hot path silently becomes queued batching.
//                                   Verified end-to-end at 6.5s with it on.
//   enableAutoStop:        false  — auto-stop halts an idle cluster. On a monitoring store
//                                   that is indistinguishable from an outage: ingestion fails
//                                   and dashboards flatline while the portal shows a healthy,
//                                   merely stopped, cluster.
//
// SKU availability is region-specific and is NOT caught by `bicep build` or by `what-if`; the
// cluster fails ~10 minutes into the deployment with "The sku X is not supported in <region>".
// Check before deploying:
//
//   az kusto cluster list-sku -o json | ConvertFrom-Json |
//     Where-Object { $_.locations -contains '<region>' } | Select-Object -ExpandProperty name

@description('Cluster name; globally unique, 4-22 lowercase alphanumeric characters.')
@minLength(4)
@maxLength(22)
param clusterName string

@description('Azure region.')
param location string = resourceGroup().location

@description('Resource tags.')
param tags object = {}

@description('''
SKU name. Verify availability in the target region first.

Dev(No SLA)_Standard_D11_v2 is single-node with no SLA — acceptable for a benchmark archive,
not for production alerting. Standard_D11_v2 or Standard_E2ads_v5 are the usual production
starting points.
''')
param skuName string = 'Standard_D11_v2'

@description('SKU tier. Basic is single-node; Standard supports multiple instances.')
@allowed(['Basic', 'Standard'])
param skuTier string = 'Standard'

@description('Instance count. Ignored on Basic. Two or more is required for the availability SLA.')
@minValue(1)
@maxValue(1000)
param capacity int = 2

@description('Enable streaming ingestion. Required by the hot path; do not disable.')
param enableStreamingIngest bool = true

@description('Enable public network access to the cluster.')
param enablePublicNetworkAccess bool = true

resource cluster 'Microsoft.Kusto/clusters@2023-08-15' = {
  name: clusterName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
    capacity: skuTier == 'Basic' ? 1 : capacity
  }
  identity: {
    // System-assigned identity so the cluster can reach other Azure resources without a
    // secret. Grafana authenticates TO this cluster using its own identity; see
    // roleAssignments.bicep.
    type: 'SystemAssigned'
  }
  properties: {
    enableStreamingIngest: enableStreamingIngest
    enableAutoStop: false
    enableDiskEncryption: true
    enablePurge: false
    publicNetworkAccess: enablePublicNetworkAccess ? 'Enabled' : 'Disabled'
  }
}

output clusterId string = cluster.id
output clusterName string = cluster.name
output clusterUri string = cluster.properties.uri
output clusterIngestUri string = cluster.properties.dataIngestionUri
output clusterPrincipalId string = cluster.identity.principalId
