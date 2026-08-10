// main.bicep — ADX store entry point.
//
// Creates the cluster, the database, and the least-privilege principal assignments. It does
// NOT create the tables: table DDL, ingestion mappings, materialized views and per-table
// policies live in adx/tables/ and adx/policies/ as .kql, and are applied with
// testing/scripts/bootstrap_adx.py. ARM cannot express them, and splitting them across two
// mechanisms would mean the schema is only half version-controlled.
//
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file main.bicep \
//     --parameters @main.parameters.json
//
//   python ../../testing/scripts/bootstrap_adx.py     # then apply the schema

targetScope = 'resourceGroup'

@description('ADX cluster name; globally unique, 4-22 lowercase alphanumeric characters.')
@minLength(4)
@maxLength(22)
param clusterName string

@description('Azure region.')
param location string = resourceGroup().location

@description('Resource tags.')
param tags object = {
  project: 'azure-mysql-monitoring'
}

@description('Database name holding MysqlMetrics and MysqlEvents.')
param databaseName string = 'mysqlmonitoring'

@description('Cluster SKU. Verify availability in the target region first.')
param skuName string = 'Standard_D11_v2'

@description('SKU tier.')
@allowed(['Basic', 'Standard'])
param skuTier string = 'Standard'

@description('Instance count. Two or more is required for the availability SLA.')
param capacity int = 2

@description('Principal ID of the collector identity granted Ingestor.')
param collectorPrincipalId string = ''

@description('Principal ID of the Grafana managed identity granted Viewer.')
param grafanaPrincipalId string = ''

@description('Object ID of a human or group needing Admin to apply schema changes.')
param operatorPrincipalId string = ''

@description('Assign the operator Admin explicitly. Not needed when they deployed the cluster.')
param assignOperatorAdmin bool = false

module cluster 'cluster.bicep' = {
  name: 'adxCluster'
  params: {
    clusterName: clusterName
    location: location
    tags: tags
    skuName: skuName
    skuTier: skuTier
    capacity: capacity
  }
}

module database 'database.bicep' = {
  name: 'adxDatabase'
  params: {
    clusterName: cluster.outputs.clusterName
    databaseName: databaseName
    location: location
  }
}

module roles 'roleAssignments.bicep' = {
  name: 'adxRoles'
  params: {
    clusterName: cluster.outputs.clusterName
    databaseName: database.outputs.databaseName
    collectorPrincipalId: collectorPrincipalId
    grafanaPrincipalId: grafanaPrincipalId
    operatorPrincipalId: operatorPrincipalId
    assignOperatorAdmin: assignOperatorAdmin
  }
}

@description('Set as ADX_CLUSTER_URI for the collector and the Grafana data source.')
output adxClusterUri string = cluster.outputs.clusterUri

@description('Set as ADX_INGEST_URI. Queued ingestion targets this; streaming targets the cluster URI.')
output adxIngestUri string = cluster.outputs.clusterIngestUri

@description('Set as ADX_DATABASE.')
output adxDatabase string = database.outputs.databaseName
