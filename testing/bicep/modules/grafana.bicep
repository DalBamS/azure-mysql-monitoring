// modules/grafana.bicep — Azure Managed Grafana (Layer 3).
//
// STANDARD tier is required. The Azure Data Explorer data source is not available on the
// deprecated Essential tier, and ADX is the primary data source for this repo — so Essential
// would deploy successfully and then be unable to show any collector data.
//
// The workspace gets a system-assigned identity. That identity is granted:
//   * Monitoring Reader on the subscription  -> Azure Monitor data source (Layer 1)
//   * AllDatabasesViewer on the ADX cluster  -> ADX data source (Layer 2 store)
// Both are read-only, and neither involves a secret.

@description('Managed Grafana workspace name.')
param grafanaName string

@description('Azure region.')
param location string

@description('Resource tags.')
param tags object = {}

@description('Object ID of the user or group to grant Grafana Admin. Empty skips the assignment.')
param adminPrincipalId string = ''

@description('Principal type for the admin assignment.')
@allowed(['User', 'Group', 'ServicePrincipal'])
param adminPrincipalType string = 'User'

@description('Grafana major version. Azure rejects retired versions, so this is a parameter.')
@allowed(['12', '13'])
param grafanaMajorVersion string = '12'

resource grafana 'Microsoft.Dashboard/grafana@2023-09-01' = {
  name: grafanaName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publicNetworkAccess: 'Enabled'
    zoneRedundancy: 'Disabled'
    apiKey: 'Disabled'
    deterministicOutboundIP: 'Disabled'
    // Azure retires Grafana major versions on its own schedule; the service currently accepts
    // only 12 and 13. Pin the lower supported version so dashboard JSON stays portable, and
    // expect this to need bumping again when 12 is retired.
    grafanaMajorVersion: grafanaMajorVersion
  }
}

// Grafana Admin, so the deploying user can actually open the workspace and add dashboards.
// Without this the workspace deploys and then refuses to load for its own creator.
var grafanaAdminRoleId = '22926164-76b3-42b3-bc55-97df8dab3e41'

resource grafanaAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(adminPrincipalId)) {
  name: guid(grafana.id, adminPrincipalId, grafanaAdminRoleId)
  scope: grafana
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grafanaAdminRoleId)
    principalId: adminPrincipalId
    principalType: adminPrincipalType
  }
}

output grafanaId string = grafana.id
output grafanaNameOut string = grafana.name
output grafanaEndpoint string = grafana.properties.endpoint
output grafanaPrincipalId string = grafana.identity.principalId
