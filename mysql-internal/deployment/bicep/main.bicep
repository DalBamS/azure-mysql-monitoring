// Production collector VM: no public NIC, outbound through a stable NAT IP.
// The managed identity receives only Key Vault secret read and ADX database ingestion.

targetScope = 'resourceGroup'

@description('Collector VM name.')
param vmName string = 'mysql-monitoring-collector'

@description('Azure region.')
param location string = resourceGroup().location

@description('Ubuntu VM SKU.')
param vmSize string = 'Standard_D2s_v5'

@description('Local administrator used only through Azure Bastion or a private network.')
param adminUsername string = 'azureadmin'

@secure()
@description('SSH public key. The VM has no public IP and no inbound Internet rule.')
param sshPublicKey string

@description('Virtual network name created for the collector.')
param virtualNetworkName string = '${vmName}-vnet'

@description('Virtual network address prefix.')
param virtualNetworkPrefix string = '10.84.0.0/16'

@description('Collector subnet address prefix.')
param subnetPrefix string = '10.84.1.0/24'

@description('Optional existing Key Vault name in this resource group.')
param keyVaultName string = ''

@description('Create the named Key Vault when true. Secret values are never deployed by this template.')
param createKeyVault bool = false

@description('Existing secret names the collector may read. Assignments are secret-scoped.')
param keyVaultSecretNames array = []

@description('Optional existing ADX cluster name in this resource group.')
param adxClusterName string = ''

@description('Optional existing ADX database receiving collector telemetry.')
param adxDatabaseName string = ''

@description('Optional existing MySQL Flexible Server name. Adds the NAT IP to its firewall.')
param mysqlServerName string = ''

@description('Tags applied to resources.')
param tags object = {
  project: 'azure-mysql-monitoring'
  component: 'collector'
}

var subnetName = 'collector'
var networkSecurityGroupName = '${vmName}-nsg'
var natGatewayName = '${vmName}-nat'
var outboundPublicIpName = '${vmName}-egress'
var networkInterfaceName = '${vmName}-nic'

resource outboundPublicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: outboundPublicIpName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2024-05-01' = {
  name: natGatewayName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [
      {
        id: outboundPublicIp.id
      }
    ]
  }
}

resource networkSecurityGroup 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: networkSecurityGroupName
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowPrivateEndpointsOutbound'
        properties: {
          priority: 90
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRanges: [
            '443'
            '3306'
          ]
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'VirtualNetwork'
        }
      }
      {
        name: 'AllowMySqlOutbound'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '3306'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'Internet'
        }
      }
      {
        name: 'AllowHttpsOutbound'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '443'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'Internet'
        }
      }
      {
        name: 'AllowPackageHttpOutbound'
        properties: {
          priority: 120
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '80'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'Internet'
        }
      }
      {
        name: 'AllowAzurePlatformDns'
        properties: {
          priority: 130
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Udp'
          sourcePortRange: '*'
          destinationPortRange: '53'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'AzurePlatformDNS'
        }
      }
      {
        name: 'AllowAzureInstanceMetadata'
        properties: {
          priority: 140
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '80'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'AzurePlatformIMDS'
        }
      }
      {
        name: 'DenyOtherOutbound'
        properties: {
          priority: 4096
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourcePortRange: '*'
          destinationPortRange: '*'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
        }
      }
    ]
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: virtualNetworkName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        virtualNetworkPrefix
      ]
    }
    subnets: [
      {
        name: subnetName
        properties: {
          addressPrefix: subnetPrefix
          defaultOutboundAccess: false
          natGateway: {
            id: natGateway.id
          }
          networkSecurityGroup: {
            id: networkSecurityGroup.id
          }
        }
      }
    ]
  }
}

resource networkInterface 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: networkInterfaceName
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'primary'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: resourceId('Microsoft.Network/virtualNetworks/subnets', virtualNetwork.name, subnetName)
          }
        }
      }
    ]
  }
}

resource collectorVm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: {
        secureBootEnabled: true
        vTpmEnabled: true
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        diskSizeGB: 64
        managedDisk: {
          storageAccountType: 'Premium_LRS'
        }
      }
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      customData: base64(loadTextContent('../cloud-init.yaml'))
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        patchSettings: {
          patchMode: 'AutomaticByPlatform'
          assessmentMode: 'AutomaticByPlatform'
          automaticByPlatformSettings: {
            rebootSetting: 'IfRequired'
          }
        }
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: networkInterface.id
          properties: {
            primary: true
          }
        }
      ]
    }
  }
}

resource createdKeyVault 'Microsoft.KeyVault/vaults@2023-07-01' = if (createKeyVault) {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
  }
}

module access 'modules/access.bicep' = if ((!empty(keyVaultName) && length(keyVaultSecretNames) > 0) || (!empty(adxClusterName) && !empty(adxDatabaseName))) {
  name: 'collectorAccess'
  params: {
    principalId: collectorVm.identity.principalId
    keyVaultName: keyVaultName
    keyVaultSecretNames: keyVaultSecretNames
    adxClusterName: adxClusterName
    adxDatabaseName: adxDatabaseName
  }
  dependsOn: [
    createdKeyVault
  ]
}

module mysqlFirewall 'modules/mysql-firewall.bicep' = if (!empty(mysqlServerName)) {
  name: 'collectorMysqlFirewall'
  params: {
    mysqlServerName: mysqlServerName
    outboundPublicIp: outboundPublicIp.properties.ipAddress
    ruleName: '${vmName}-egress'
  }
}

output vmResourceId string = collectorVm.id
output collectorPrincipalId string = collectorVm.identity.principalId
output collectorPrivateIp string = networkInterface.properties.ipConfigurations[0].properties.privateIPAddress
output outboundPublicIp string = outboundPublicIp.properties.ipAddress
output keyVaultUri string = !empty(keyVaultName) ? 'https://${keyVaultName}${environment().suffixes.keyvaultDns}' : ''
