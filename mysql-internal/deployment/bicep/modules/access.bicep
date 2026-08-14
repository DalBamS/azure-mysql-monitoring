targetScope = 'resourceGroup'

@description('Collector managed identity principal ID.')
param principalId string

@description('Existing Key Vault name. Empty skips the role assignment.')
param keyVaultName string = ''

@description('Existing secret names the collector identity may read.')
param keyVaultSecretNames array = []

@description('Existing ADX cluster name. Empty skips the principal assignment.')
param adxClusterName string = ''

@description('Existing ADX database name. Empty skips the principal assignment.')
param adxDatabaseName string = ''

var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = if (!empty(keyVaultName)) {
  name: keyVaultName
}

resource keyVaultSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = [
  for secretName in keyVaultSecretNames: {
    parent: keyVault
    name: secretName
  }
]

resource keyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (secretName, index) in keyVaultSecretNames: {
  name: guid(keyVaultSecrets[index].id, principalId, keyVaultSecretsUserRoleId)
  scope: keyVaultSecrets[index]
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultSecretsUserRoleId
    )
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}]

resource adxCluster 'Microsoft.Kusto/clusters@2023-08-15' existing = if (!empty(adxClusterName) && !empty(adxDatabaseName)) {
  name: adxClusterName
}

resource adxDatabase 'Microsoft.Kusto/clusters/databases@2023-08-15' existing = if (!empty(adxClusterName) && !empty(adxDatabaseName)) {
  parent: adxCluster
  name: adxDatabaseName
}

resource collectorIngestor 'Microsoft.Kusto/clusters/databases/principalAssignments@2023-08-15' = if (!empty(adxClusterName) && !empty(adxDatabaseName)) {
  parent: adxDatabase
  name: guid(adxDatabase.id, principalId, 'Ingestor')
  properties: {
    principalId: principalId
    principalType: 'App'
    role: 'Ingestor'
    tenantId: tenant().tenantId
  }
}
