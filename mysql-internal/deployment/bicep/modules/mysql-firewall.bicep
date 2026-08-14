targetScope = 'resourceGroup'

@description('Existing Azure Database for MySQL Flexible Server name.')
param mysqlServerName string

@description('Collector NAT gateway public IPv4 address.')
param outboundPublicIp string

@description('Firewall rule name.')
param ruleName string

resource mysqlServer 'Microsoft.DBforMySQL/flexibleServers@2023-12-30' existing = {
  name: mysqlServerName
}

resource collectorRule 'Microsoft.DBforMySQL/flexibleServers/firewallRules@2023-12-30' = {
  parent: mysqlServer
  name: ruleName
  properties: {
    startIpAddress: outboundPublicIp
    endIpAddress: outboundPublicIp
  }
}
