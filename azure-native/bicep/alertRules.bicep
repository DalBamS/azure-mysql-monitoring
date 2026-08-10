// alertRules.bicep — Layer 1 alerting: the independent safety net.
//
// These rules exist BECAUSE the fast path can fail. Layer 2 -> ADX -> Grafana detects problems
// in ~25-45s, but every stage of it is something this repository operates: a collector that
// crashes, an expired credential or a broken ingestion policy takes the fast path down
// silently. Azure Monitor evaluates server-side and keeps working when all of that is broken,
// at the cost of a 2-5 minute lag.
//
// So these are deliberately NOT a mirror of the Grafana rules. They cover conditions that are
// still true when the collector is gone, and they are tuned to avoid double-paging: thresholds
// sit at "this is genuinely wrong" rather than "this is worth a chart".
//
// Metric names below were read from a live server with
// `az monitor metrics list-definitions`. They are case-sensitive and several use MySQL's own
// capitalisation (Slow_queries, Threads_running) rather than Azure's snake_case.

@description('Name of the existing MySQL Flexible Server to alert on.')
param mysqlServerName string

@description('Resource tags.')
param tags object = {}

@description('Action group resource ID to notify. Empty creates the rules without an action.')
param actionGroupId string = ''

@description('Deploy the alert rules in a disabled state, for staged rollout.')
param alertsEnabled bool = true

@description('CPU percentage that must be sustained before alerting.')
param cpuThreshold int = 85

@description('Memory percentage that must be sustained before alerting.')
param memoryThreshold int = 90

@description('Storage percentage that triggers a warning. Storage-full takes a server offline.')
param storageThreshold int = 85

@description('Aborted connections per evaluation window before alerting.')
param abortedConnectionsThreshold int = 10

@description('Replication lag in seconds before alerting. Ignored on servers without a replica.')
param replicationLagThreshold int = 60

@description('Deploy the replication lag rule. Meaningless without a read replica.')
param deployReplicationAlert bool = false

resource mysqlServer 'Microsoft.DBforMySQL/flexibleServers@2023-12-30' existing = {
  name: mysqlServerName
}

var actions = empty(actionGroupId) ? [] : [
  {
    actionGroupId: actionGroupId
  }
]

// Severity 2 = warning, 1 = error. Storage is severity 1 because a full data disk stops the
// server entirely and cannot be recovered from by shedding load.
var rules = [
  {
    name: 'mysql-cpu-high'
    description: 'CPU sustained above threshold for 15 minutes. Layer 1 safety net; the Grafana rule fires sooner when the collector is alive.'
    metric: 'cpu_percent'
    operator: 'GreaterThan'
    threshold: cpuThreshold
    aggregation: 'Average'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
  }
  {
    name: 'mysql-memory-high'
    description: 'Memory sustained above threshold. On Burstable tiers this precedes OOM-driven restarts.'
    metric: 'memory_percent'
    operator: 'GreaterThan'
    threshold: memoryThreshold
    aggregation: 'Average'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
  }
  {
    name: 'mysql-storage-high'
    description: 'Storage approaching capacity. A full data disk takes the server offline and cannot be resolved by shedding load.'
    metric: 'storage_percent'
    operator: 'GreaterThan'
    threshold: storageThreshold
    aggregation: 'Average'
    severity: 1
    windowSize: 'PT15M'
    evaluationFrequency: 'PT15M'
  }
  {
    name: 'mysql-aborted-connections'
    description: 'Aborted connections climbing. On MySQL 8.4 a common cause is a client that cannot do caching_sha2_password or refuses TLS.'
    metric: 'aborted_connections'
    operator: 'GreaterThan'
    threshold: abortedConnectionsThreshold
    aggregation: 'Total'
    severity: 2
    windowSize: 'PT5M'
    evaluationFrequency: 'PT5M'
  }
]

resource metricAlerts 'Microsoft.Insights/metricAlerts@2018-03-01' = [for rule in rules: {
  name: '${mysqlServerName}-${rule.name}'
  location: 'global'
  tags: tags
  properties: {
    description: rule.description
    severity: rule.severity
    enabled: alertsEnabled
    scopes: [mysqlServer.id]
    evaluationFrequency: rule.evaluationFrequency
    windowSize: rule.windowSize
    targetResourceType: 'Microsoft.DBforMySQL/flexibleServers'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'criterion1'
          metricName: rule.metric
          metricNamespace: 'Microsoft.DBforMySQL/flexibleServers'
          operator: rule.operator
          threshold: rule.threshold
          timeAggregation: rule.aggregation
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    autoMitigate: true
    actions: actions
  }
}]

// Replication lag is opt-in: on a server with no replica the metric never reports, and an
// alert on a metric that never arrives is silent in exactly the way that matters.
resource replicationAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = if (deployReplicationAlert) {
  name: '${mysqlServerName}-mysql-replication-lag'
  location: 'global'
  tags: tags
  properties: {
    description: 'Read replica falling behind the primary.'
    severity: 1
    enabled: alertsEnabled
    scopes: [mysqlServer.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    targetResourceType: 'Microsoft.DBforMySQL/flexibleServers'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'criterion1'
          metricName: 'replication_lag'
          metricNamespace: 'Microsoft.DBforMySQL/flexibleServers'
          operator: 'GreaterThan'
          threshold: replicationLagThreshold
          timeAggregation: 'Maximum'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    autoMitigate: true
    actions: actions
  }
}

output alertRuleNames array = [for (rule, i) in rules: metricAlerts[i].name]
