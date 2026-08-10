# Domain context

## Product

`azure-mysql-monitoring` is a general-purpose monitoring system for services that use
**Azure Database for MySQL Flexible Server 8.4**.

The normal deployment is:

```text
Azure MySQL targets -> collector VM -> Azure Data Explorer -> Grafana
```

One collector VM monitors multiple MySQL targets. Production monitoring is the primary operating
mode. Premium SSD v1 versus Premium SSD v2 analysis reuses the same telemetry contract and
dashboards, adding a `RUN_ID` and storage-tier dimension rather than introducing a separate
benchmark pipeline.

## Domain language

### Target

One Azure Database for MySQL Flexible Server instance monitored by the collector. A Target has a
stable ID, endpoint, Azure resource ID, storage tier, collection Profile and credential references.

### Collector VM

The Azure VM that runs the collector runtime for multiple Targets. It owns scheduling, MySQL
connections, local durable spool, cursor state and collector health telemetry. It never stores a
literal MySQL password in its configuration.

### Telemetry Point

The canonical collector output. A Telemetry Point contains:

- a measurement identifying the measured domain;
- tags identifying dimensions such as Target, schema, table, index, digest, event or storage tier;
- fields containing observed values;
- the observation timestamp, `RUN_ID` and contract version.

Fields retain their semantic kind (`counter`, `gauge`, `state`, `event`) and unit. Counter rates
are derived reset-safely after collection.

### Measurement

A stable family of related fields gathered together, such as `mysql.global_status`,
`mysql.file_io`, `mysql.statement_digest` or `collector.health`.

### Collection Group

The implementation that gathers one Measurement or a closely related set of Measurements. It owns
its SQL, result mapping, capability checks, default cadence, privilege requirements and
cardinality limit.

### Collection Plan

The validated, executable result of combining Targets with Profiles. It determines which
Collection Groups run for each Target and when. The runtime executes the Collection Plan; it does
not contain hardcoded knowledge of individual groups.

### Profile

A named selection of Collection Groups and safe overrides. Profiles express monitoring intent:

- `standard` — general production health, enabled by default;
- `extended` — deeper query and storage diagnosis at moderate cost;
- `deep-dive` — opt-in, high-cardinality schema/table/index diagnosis;
- `benchmark` — short cadence for QPS, IOPS and latency, tagged with a benchmark `RUN_ID`.

### Metric Catalog

The repository-owned source of truth for measurement names, fields, kinds, units, dimensions,
cardinality, default cadence and derived semantics. Collector adapters, ADX query functions and
Grafana dashboards must use catalog identities rather than inventing metric strings independently.

### High-cardinality telemetry

Telemetry dimensioned by digest, schema, table, index, user or process state. It is never enabled
implicitly. A Collection Plan must opt in and apply a bound such as top-K, schema filtering or an
interval floor.

### Durable spool

The collector VM's local append-only JSONL recovery path. It preserves telemetry while ADX is
unavailable and is replayed through the same Telemetry Point contract after recovery.

### Operational dashboard

A Grafana dashboard organized around an operator question: availability, workload, queries,
InnoDB, locks and transactions, storage IO and latency, capacity, errors, or collector health.

### Comparison view

A Grafana view that compares the same measurements across two `RUN_ID` values or time windows.
Premium SSD v1/v2 comparison is one use of this view, not a separate telemetry model.

## Data lifecycle

All telemetry stored in ADX — raw points, events, dimensional data, materialized views and rollups —
is deleted after **90 days**. No ADX artifact in this repository may preserve telemetry beyond
that period.

