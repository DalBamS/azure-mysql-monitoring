// roleAssignments.bicep — least-privilege access to the store.
//
// Three principals, three different levels. Getting this wrong is the most common way a
// monitoring stack ends up with an over-privileged service account:
//
//   collector -> Ingestor  can write rows, cannot read them back or alter the schema
//   grafana   -> Viewer    read-only, and only over this one database
//   operator  -> Admin     applies KQL DDL; usually already granted implicitly (see below)
//
// The collector deliberately gets Ingestor rather than Admin. It is the component with the
// widest network exposure — it holds MySQL credentials and runs on a benchmark host — so a
// compromise should not be able to read back the audit trail or drop a table.

@description('ADX cluster name.')
param clusterName string

@description('ADX database name.')
param databaseName string

@description('Tenant ID owning the principals.')
param tenantId string = tenant().tenantId

@description('Principal ID of the collector identity. Empty skips the assignment.')
param collectorPrincipalId string = ''

@description('Principal type of the collector identity. Managed identities are "App".')
@allowed(['App', 'User', 'Group'])
param collectorPrincipalType string = 'App'

@description('Principal ID of the Grafana managed identity. Empty skips the assignment.')
param grafanaPrincipalId string = ''

@description('Object ID of a human or group needing Admin to apply schema changes.')
param operatorPrincipalId string = ''

@description('Principal type of the operator.')
@allowed(['User', 'Group', 'App'])
param operatorPrincipalType string = 'User'

@description('''
Explicitly assign the operator Admin on the database.

Leave this false when the operator is also the principal that deployed the cluster: ADX grants
the database creator Admin automatically, under a server-generated GUID name. A second
assignment for the same principal and role is rejected with "A PrincipalAssignment resource
already exists with the same role and principal id", which fails the deployment after the
cluster has already been paid for. Set it true only when the operator differs from the deployer.
''')
param assignOperatorAdmin bool = false

resource cluster 'Microsoft.Kusto/clusters@2023-08-15' existing = {
  name: clusterName
}

resource database 'Microsoft.Kusto/clusters/databases@2023-08-15' existing = {
  parent: cluster
  name: databaseName
}

resource collectorIngestor 'Microsoft.Kusto/clusters/databases/principalAssignments@2023-08-15' = if (!empty(collectorPrincipalId)) {
  parent: database
  name: guid(database.id, collectorPrincipalId, 'Ingestor')
  properties: {
    principalId: collectorPrincipalId
    principalType: collectorPrincipalType
    role: 'Ingestor'
    tenantId: tenantId
  }
}

// Grafana reads and nothing else. A compromised dashboard cannot drop a table.
// Principal assignments on one database are applied serially by the service, so these are
// chained rather than deployed in parallel.
resource grafanaViewer 'Microsoft.Kusto/clusters/databases/principalAssignments@2023-08-15' = if (!empty(grafanaPrincipalId)) {
  parent: database
  name: guid(database.id, grafanaPrincipalId, 'Viewer')
  properties: {
    principalId: grafanaPrincipalId
    principalType: 'App'
    role: 'Viewer'
    tenantId: tenantId
  }
  dependsOn: [collectorIngestor]
}

resource operatorAdmin 'Microsoft.Kusto/clusters/databases/principalAssignments@2023-08-15' = if (assignOperatorAdmin && !empty(operatorPrincipalId)) {
  parent: database
  name: guid(database.id, operatorPrincipalId, 'Admin')
  properties: {
    principalId: operatorPrincipalId
    principalType: operatorPrincipalType
    role: 'Admin'
    tenantId: tenantId
  }
  dependsOn: [grafanaViewer]
}
