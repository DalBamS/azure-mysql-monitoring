// main.bicep — Layer 1 entry point.
//
// Attaches Azure Monitor telemetry to an EXISTING MySQL Flexible Server. This is the
// difference between this template and testing/bicep/main.bicep, which creates a throwaway
// server: production servers already exist, and monitoring must never be able to recreate,
// resize or restart the thing it is monitoring. Nothing here mutates the server itself.
//
// Deploy into the resource group that holds the MySQL server:
//
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file main.bicep \
//     --parameters @main.parameters.json

targetScope = 'resourceGroup'

@description('Name of the existing MySQL Flexible Server to monitor.')
param mysqlServerName string

@description('Azure region for the workspace. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Resource tags applied to created resources.')
param tags object = {
  project: 'azure-mysql-monitoring'
}

@description('Create a Log Analytics workspace. Set false to reuse an existing one.')
param createWorkspace bool = true

@description('Workspace name when creating one.')
param workspaceName string = '${mysqlServerName}-law'

@description('Resource ID of an existing workspace. Required when createWorkspace is false.')
param existingWorkspaceId string = ''

@description('Workspace retention in days.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

@description('Daily ingestion cap in GB. 0 disables the cap.')
param dailyQuotaGb int = 0

@description('Deploy the Layer 1 metric alert rules.')
param deployAlerts bool = true

@description('Action group resource ID for alert notifications. Empty creates rules with no action.')
param actionGroupId string = ''

@description('Deploy the replication lag alert. Only meaningful with a read replica.')
param deployReplicationAlert bool = false

module logAnalytics 'logAnalytics.bicep' = if (createWorkspace) {
  name: 'logAnalytics'
  params: {
    workspaceName: workspaceName
    location: location
    tags: tags
    retentionInDays: retentionInDays
    dailyQuotaGb: dailyQuotaGb
  }
}

// Fails fast rather than deploying a diagnostic setting pointed at an empty workspace ID,
// which ARM accepts and which then silently discards every log.
var resolvedWorkspaceId = createWorkspace ? (logAnalytics.?outputs.workspaceId ?? '') : existingWorkspaceId

module diagnosticSettings 'diagnosticSettings.bicep' = {
  name: 'diagnosticSettings'
  params: {
    mysqlServerName: mysqlServerName
    workspaceId: resolvedWorkspaceId
  }
}

module alertRules 'alertRules.bicep' = if (deployAlerts) {
  name: 'alertRules'
  params: {
    mysqlServerName: mysqlServerName
    tags: tags
    actionGroupId: actionGroupId
    deployReplicationAlert: deployReplicationAlert
  }
}

output workspaceId string = resolvedWorkspaceId
output workspaceCustomerId string = createWorkspace ? (logAnalytics.?outputs.workspaceCustomerId ?? '') : ''
output diagnosticSettingId string = diagnosticSettings.outputs.diagnosticSettingId
