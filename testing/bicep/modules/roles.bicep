// modules/roles.bicep — least-privilege identities for the test environment.
//
// Three principals, three different levels of access. Getting this wrong is the most common
// way a monitoring stack ends up with an over-privileged service account:
//
//   collector -> Ingestor  : can write rows, cannot read them back or alter the schema
//   operator  -> Admin     : the deploying human, needs to apply KQL DDL and run verify.py
//   grafana   -> Viewer    : read-only, and only over the one database
//
// Azure Monitor access for Grafana is scoped to this resource group, not the subscription.

@description('ADX cluster name.')
param clusterName string

@description('ADX database name.')
param databaseName string

@description('Object ID of the deploying operator (applies schema, runs verification).')
param operatorPrincipalId string

@description('Principal type of the operator.')
@allowed(['User', 'Group', 'ServicePrincipal'])
param operatorPrincipalType string = 'User'

@description('Grafana managed identity principal ID. Empty skips the Grafana assignments.')
param grafanaPrincipalId string = ''

@description('Tenant ID owning the principals.')
param tenantId string = tenant().tenantId

resource cluster 'Microsoft.Kusto/clusters@2023-08-15' existing = {
  name: clusterName
}

resource database 'Microsoft.Kusto/clusters/databases@2023-08-15' existing = {
  parent: cluster
  name: databaseName
}

// The operator applies table DDL, mappings, policies and materialized views, then queries
// them back during verification. That requires Admin on the database — but only on this
// database, never on the cluster.
resource operatorAdmin 'Microsoft.Kusto/clusters/databases/principalAssignments@2023-08-15' = {
  parent: database
  name: guid(database.id, operatorPrincipalId, 'Admin')
  properties: {
    principalId: operatorPrincipalId
    principalType: operatorPrincipalType
    role: 'Admin'
    tenantId: tenantId
  }
}

// Grafana reads and nothing else. A compromised dashboard cannot drop a table.
resource grafanaViewer 'Microsoft.Kusto/clusters/databases/principalAssignments@2023-08-15' = if (!empty(grafanaPrincipalId)) {
  parent: database
  name: guid(database.id, grafanaPrincipalId, 'Viewer')
  properties: {
    principalId: grafanaPrincipalId
    principalType: 'App'
    role: 'Viewer'
    tenantId: tenantId
  }
  dependsOn: [operatorAdmin]
}

// Monitoring Reader lets the Grafana Azure Monitor data source query Log Analytics and
// platform metrics. Scoped to the resource group so Grafana cannot read the whole
// subscription's telemetry.
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'

resource grafanaMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(grafanaPrincipalId)) {
  name: guid(resourceGroup().id, grafanaPrincipalId, monitoringReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: grafanaPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output operatorAssignmentId string = operatorAdmin.id
