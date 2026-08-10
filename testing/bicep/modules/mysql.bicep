// modules/mysql.bicep — Azure Database for MySQL Flexible Server (MySQL 8.4).
//
// Burstable B1ms with the smallest supported disk: this is a monitoring test target, not a
// performance target. Premium SSD v2 is deliberately NOT used here — it is in preview,
// requires regional enablement, and the point of the test environment is to prove the
// monitoring pipeline works, not to benchmark storage.

@description('Server name; must be globally unique within the MySQL DNS namespace.')
param serverName string

@description('Azure region.')
param location string

@description('Administrator login. Never a literal in source control.')
param administratorLogin string

@description('Administrator password, supplied at deploy time.')
@secure()
param administratorPassword string

@description('Initial database created for the test workload.')
param databaseName string

@description('Resource tags.')
param tags object = {}

@description('Client IPv4 address allowed through the server firewall.')
param clientIpAddress string

var mysqlVersion = '8.4'

resource server 'Microsoft.DBforMySQL/flexibleServers@2023-12-30' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: mysqlVersion
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    createMode: 'Default'
    storage: {
      // 20 GB is the minimum. autoGrow is disabled so a runaway test cannot silently
      // enlarge the disk and the bill along with it.
      storageSizeGB: 20
      iops: 360
      autoGrow: 'Disabled'
      autoIoScaling: 'Disabled'
    }
    backup: {
      backupRetentionDays: 1
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

// Azure enforces require_secure_transport=ON by default. It is asserted here rather than
// assumed: the collector refuses to run over plaintext, so a drift in this parameter would
// otherwise surface as a confusing connection failure.
resource requireSecureTransport 'Microsoft.DBforMySQL/flexibleServers/configurations@2023-12-30' = {
  parent: server
  name: 'require_secure_transport'
  properties: {
    value: 'ON'
    source: 'user-override'
  }
}

// Slow query logging feeds the ONE diagnostic category that carries query text.
// long_query_time is dropped to 0 so the test workload reliably produces rows; a production
// server would never use 0.
resource slowQueryLog 'Microsoft.DBforMySQL/flexibleServers/configurations@2023-12-30' = {
  parent: server
  name: 'slow_query_log'
  properties: {
    value: 'ON'
    source: 'user-override'
  }
  dependsOn: [requireSecureTransport]
}

resource longQueryTime 'Microsoft.DBforMySQL/flexibleServers/configurations@2023-12-30' = {
  parent: server
  name: 'long_query_time'
  properties: {
    value: '0'
    source: 'user-override'
  }
  dependsOn: [slowQueryLog]
}

// Audit logging must be switched on at the server AND selected in the diagnostic setting.
// Enabling only the diagnostic category produces an empty table and looks like a broken
// pipeline, which is exactly the false negative this test environment must not have.
resource auditLogEnabled 'Microsoft.DBforMySQL/flexibleServers/configurations@2023-12-30' = {
  parent: server
  name: 'audit_log_enabled'
  properties: {
    value: 'ON'
    source: 'user-override'
  }
  dependsOn: [longQueryTime]
}

resource auditLogEvents 'Microsoft.DBforMySQL/flexibleServers/configurations@2023-12-30' = {
  parent: server
  name: 'audit_log_events'
  properties: {
    value: 'CONNECTION,DDL,DML'
    source: 'user-override'
  }
  dependsOn: [auditLogEnabled]
}

resource database 'Microsoft.DBforMySQL/flexibleServers/databases@2023-12-30' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'utf8mb4'
    collation: 'utf8mb4_0900_ai_ci'
  }
}

// Allows the machine running the collector and the verification script to connect.
resource clientFirewall 'Microsoft.DBforMySQL/flexibleServers/firewallRules@2023-12-30' = {
  parent: server
  name: 'allow-test-client'
  properties: {
    startIpAddress: clientIpAddress
    endIpAddress: clientIpAddress
  }
}

output serverId string = server.id
output fqdn string = server.properties.fullyQualifiedDomainName
output serverNameOut string = server.name
output databaseNameOut string = database.name
output mysqlVersionOut string = mysqlVersion
