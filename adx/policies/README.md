# adx/policies/ — retention, caching and ingestion policies

The policies that make a growing production log volume affordable, and that make real-time
monitoring actually real-time.

## Expected contents

| File | Purpose |
|---|---|
| `streaming-ingestion.kql` | Enables the hot path |
| `batching.kql` | Tunes the queued (cold) path |
| `retention-caching.kql` | Retention and hot-cache tiers per table |
| `update-policies.kql` | Any ingest-time derivation |

## 1. Streaming ingestion — the hot path

Queued ingestion batches for **up to 5 minutes by default**, which cannot support real-time
monitoring. Enable streaming ingestion on the cluster (see [`../bicep/`](../bicep/)) and then per
table:

```kusto
.alter table MysqlMetrics policy streamingingestion enable
.alter table MysqlEvents  policy streamingingestion enable
```

This brings ingestion latency down to seconds. Constraints worth knowing before relying on it:

- **~4 MB per request** — large backfills must use the queued path instead.
- Streaming costs more cluster CPU per row than batched ingestion, so it is for the live stream,
  not for bulk replay.

## 2. Batching — the cold path

The cold path handles JSONL replay and benchmark archives, where throughput matters more than
latency. Shrinking the batching window trades ingestion efficiency for freshness:

```kusto
.alter table MysqlMetrics policy ingestionbatching
'{"MaximumBatchingTimeSpan":"00:00:30","MaximumNumberOfItems":1000,"MaximumRawDataSizeMB":1024}'
```

Do **not** try to achieve real-time by shrinking this alone — that produces many small extents and
degrades cluster performance. Real-time is the streaming path's job.

## 3. Retention and caching

Hot cache controls query speed and cost; retention controls how long data exists. Every telemetry
representation follows the same 90-day lifecycle. Hot-cache periods may differ because they affect
cost and performance, not deletion.

```kusto
// Raw metrics: recent data stays hot; all data expires after 90 days
.alter table MysqlMetrics policy caching   hot = 7d
.alter table MysqlMetrics policy retention '{"SoftDeletePeriod":"90.00:00:00","Recoverability":"Disabled"}'

// 1-minute rollups: cheaper long-range queries inside the same lifecycle
.alter materialized-view MysqlMetrics1m policy caching   hot = 90d
.alter materialized-view MysqlMetrics1m policy retention '{"SoftDeletePeriod":"90.00:00:00","Recoverability":"Disabled"}'

// Error log events: text stays hot for the incident-response window
.alter table MysqlEvents policy caching   hot = 14d
.alter table MysqlEvents policy retention '{"SoftDeletePeriod":"90.00:00:00","Recoverability":"Disabled"}'

// Benchmark metadata follows the same lifecycle
.alter table BenchmarkRuns policy retention '{"SoftDeletePeriod":"90.00:00:00","Recoverability":"Disabled"}'
```

Review hot-cache periods and collection Profiles against actual volume before production rollout.
The 90-day deletion requirement is not a cost-tuning knob.

## 4. Collector heartbeat

A self-built collector introduces a failure mode the platform does not have: **if the collector
dies, charts flatline and look healthy.** Silence must be treated as failure.

The collector emits a `collector_heartbeat` metric every cycle, and an alert fires when it stops:

```kusto
MysqlMetrics
| where Metric == "collector_heartbeat" and Timestamp > ago(5m)
| summarize LastSeen = max(Timestamp) by Host
| where LastSeen < ago(60s)
```

Back this up with a Layer 1 platform alert from [`../../azure-native/alerts/`](../../azure-native/alerts/),
so collector failure is still detected when the whole Layer 2 path is down.

## Conventions

- Policies are **applied from this repo**, not set by hand in the Kusto UI.
- Any retention change is reviewed in a PR — shortening retention is destructive.
- No credentials, cluster URIs, or customer identifiers in these files; parameterise them.
