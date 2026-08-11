targetScope = 'resourceGroup'

@description('Globally unique prefix for both benchmark servers.')
@minLength(3)
@maxLength(20)
param namePrefix string

@description('Azure region where Premium SSD v2 preview is enabled.')
param location string = resourceGroup().location

@description('MySQL administrator login.')
param administratorLogin string

@description('Generated MySQL administrator password.')
@secure()
param administratorPassword string

@description('Database created on both targets.')
param databaseName string = 'benchmark'

@description('Closest v1-compatible 8-vCore/64-GiB SKU.')
param baselineSkuName string = 'Standard_E8ds_v5'

@description('Premium SSD v2 requires a v6 compute SKU.')
param candidateSkuName string = 'Standard_E8ds_v6'

@description('Identical storage capacity in GiB.')
@minValue(32)
param storageSizeGB int = 64

@description('Premium SSD v2 provisioned IOPS.')
@minValue(3000)
param candidateIops int = 5000

@description('IPv4 addresses allowed to run the collector and workload.')
param allowedClientIps array

var tags = {
  project: 'azure-mysql-monitoring'
  environment: 'benchmark'
  purpose: 'premium-ssd-v1-vs-v2'
  retention: 'ephemeral-delete-after-test'
}

// The service does not permit Premium SSD v1 on v6 or Premium SSD v2 on v5. Both servers have
// 8 vCPUs and 64 GiB memory, but the unavoidable CPU-generation difference must be disclosed.
resource baseline 'Microsoft.DBforMySQL/flexibleServers@2023-12-30' = {
  name: '${namePrefix}-v1'
  location: location
  tags: union(tags, {
    storageTier: 'premium-ssd-v1'
  })
  sku: {
    name: baselineSkuName
    tier: 'MemoryOptimized'
  }
  properties: {
    version: '8.4'
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    createMode: 'Default'
    storage: {
      storageSizeGB: storageSizeGB
      autoGrow: 'Disabled'
      autoIoScaling: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

// Premium SSD v2 is currently subscription-gated and accepted by the latest preview API.
#disable-next-line BCP081
resource candidate 'Microsoft.DBforMySQL/flexibleServers@2025-12-01-preview' = {
  name: '${namePrefix}-v2'
  location: location
  tags: union(tags, {
    storageTier: 'premium-ssd-v2'
  })
  sku: {
    name: candidateSkuName
    tier: 'MemoryOptimized'
  }
  properties: {
    version: '8.4'
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    createMode: 'Default'
    storage: {
      storageSizeGB: storageSizeGB
      iops: candidateIops
      autoGrow: 'Disabled'
      autoIoScaling: 'Disabled'
      storageSku: 'PremiumV2_LRS'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource baselineSecureTransport 'Microsoft.DBforMySQL/flexibleServers/configurations@2023-12-30' = {
  parent: baseline
  name: 'require_secure_transport'
  properties: {
    value: 'ON'
    source: 'user-override'
  }
}

resource candidateSecureTransport 'Microsoft.DBforMySQL/flexibleServers/configurations@2025-12-01-preview' = {
  parent: candidate
  name: 'require_secure_transport'
  properties: {
    value: 'ON'
    source: 'user-override'
  }
}

resource baselineDatabase 'Microsoft.DBforMySQL/flexibleServers/databases@2023-12-30' = {
  parent: baseline
  name: databaseName
  properties: {
    charset: 'utf8mb4'
    collation: 'utf8mb4_0900_ai_ci'
  }
}

resource candidateDatabase 'Microsoft.DBforMySQL/flexibleServers/databases@2025-12-01-preview' = {
  parent: candidate
  name: databaseName
  properties: {
    charset: 'utf8mb4'
    collation: 'utf8mb4_0900_ai_ci'
  }
}

resource baselineFirewallRules 'Microsoft.DBforMySQL/flexibleServers/firewallRules@2023-12-30' = [for (ip, ipIndex) in allowedClientIps: {
  parent: baseline
  name: 'allow-client-${ipIndex}'
  properties: {
    startIpAddress: ip
    endIpAddress: ip
  }
}]

resource candidateFirewallRules 'Microsoft.DBforMySQL/flexibleServers/firewallRules@2025-12-01-preview' = [for (ip, ipIndex) in allowedClientIps: {
  parent: candidate
  name: 'allow-client-${ipIndex}'
  properties: {
    startIpAddress: ip
    endIpAddress: ip
  }
}]

output baselineHost string = baseline.properties.fullyQualifiedDomainName
output baselineServerName string = baseline.name
output candidateHost string = candidate.properties.fullyQualifiedDomainName
output candidateServerName string = candidate.name
output database string = databaseName
output baselineSku string = baselineSkuName
output candidateSku string = candidateSkuName
output storageGiB int = storageSizeGB
output candidateIops int = candidateIops
