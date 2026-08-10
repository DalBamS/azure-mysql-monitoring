// main.bicep — azure-mysql-monitoring test environment.
//
// Deploys a REAL Azure environment for verifying that the monitoring pipeline works end to
// end. Deliberately tiered so the expensive parts are opt-in:
//
//   stage 1 (default)   MySQL Flexible Server 8.4 + Log Analytics + diagnostic settings
//                       -> proves Layer 1 collection and Layer 2 connectivity
//   stage 2 (deployAdx) + Azure Data Explorer cluster and database
//                       -> proves the unified store and the streaming hot path
//   stage 3 (deployGrafana) + Azure Managed Grafana (Standard)
//                       -> proves the final view reads both data sources
//
// The ADX cluster and the Grafana workspace are the two resources that cost real money while
// idle. Run testing/scripts/teardown.ps1 when finished.
//
// Deploy:
//   az deployment group create -g <rg> -f main.bicep -p @main.parameters.json

targetScope = 'resourceGroup'

@description('Short prefix for every resource name. Lowercase alphanumeric.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'mysqlmon'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('MySQL administrator login.')
param administratorLogin string

@description('MySQL administrator password. Supplied at deploy time, never stored in the repo.')
@secure()
param administratorPassword string

@description('Database created for the test workload.')
param databaseName string = 'monitoring_test'

@description('Public IPv4 address of the machine running the collector and verification script.')
param clientIpAddress string

@description('Object ID of the deploying operator. Granted ADX Admin and Grafana Admin.')
param operatorPrincipalId string

@description('Principal type of the operator.')
@allowed(['User', 'Group', 'ServicePrincipal'])
param operatorPrincipalType string = 'User'

@description('Deploy the Azure Data Explorer cluster. Costs money while running.')
param deployAdx bool = true

@description('Deploy Azure Managed Grafana (Standard). Costs money while running.')
param deployGrafana bool = true

// A short suffix keeps globally-unique names (MySQL FQDN, ADX cluster) collision-free
// across repeated deploy/teardown cycles in the same subscription.
var suffix = substring(uniqueString(resourceGroup().id), 0, 5)

var tags = {
  project: 'azure-mysql-monitoring'
  environment: 'test'
  purpose: 'monitoring-pipeline-verification'
  costCenter: 'ephemeral-delete-when-done'
}

module mysql 'modules/mysql.bicep' = {
  name: 'mysql'
  params: {
    serverName: '${namePrefix}-mysql-${suffix}'
    location: location
    administratorLogin: administratorLogin
    administratorPassword: administratorPassword
    databaseName: databaseName
    clientIpAddress: clientIpAddress
    tags: tags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    workspaceName: '${namePrefix}-law-${suffix}'
    location: location
    mysqlServerId: mysql.outputs.serverId
    tags: tags
  }
}

module adx 'modules/adx.bicep' = if (deployAdx) {
  name: 'adx'
  params: {
    // ADX cluster names are limited to 22 lowercase alphanumeric characters, no hyphens.
    clusterName: toLower('${namePrefix}adx${suffix}')
    location: location
    databaseName: 'mysqlmonitoring'
    tags: tags
  }
}

module grafana 'modules/grafana.bicep' = if (deployGrafana) {
  name: 'grafana'
  params: {
    grafanaName: '${namePrefix}-grafana-${suffix}'
    location: location
    adminPrincipalId: operatorPrincipalId
    adminPrincipalType: operatorPrincipalType
    tags: tags
  }
}

module roles 'modules/roles.bicep' = if (deployAdx) {
  name: 'roles'
  params: {
    clusterName: adx.?outputs.clusterNameOut ?? ''
    databaseName: adx.?outputs.databaseNameOut ?? ''
    operatorPrincipalId: operatorPrincipalId
    operatorPrincipalType: operatorPrincipalType
    grafanaPrincipalId: grafana.?outputs.grafanaPrincipalId ?? ''
  }
}

// Outputs are consumed by testing/scripts/deploy.ps1, which turns them into the environment
// variables the collector and verify.py expect. No value here is a secret.
output mysqlHost string = mysql.outputs.fqdn
output mysqlServerName string = mysql.outputs.serverNameOut
output mysqlDatabase string = mysql.outputs.databaseNameOut
output mysqlVersion string = mysql.outputs.mysqlVersionOut

output workspaceId string = monitoring.outputs.workspaceId
output workspaceCustomerId string = monitoring.outputs.workspaceCustomerId

output adxDeployed bool = deployAdx
output adxClusterUri string = adx.?outputs.clusterUri ?? ''
output adxIngestUri string = adx.?outputs.ingestUri ?? ''
output adxDatabase string = adx.?outputs.databaseNameOut ?? ''

output grafanaDeployed bool = deployGrafana
output grafanaEndpoint string = grafana.?outputs.grafanaEndpoint ?? ''
output grafanaName string = grafana.?outputs.grafanaNameOut ?? ''
