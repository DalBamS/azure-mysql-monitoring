# ADR-0001: General-purpose Azure MySQL monitoring architecture

## Status

Accepted

## Context

The repository began with a narrow benchmark goal: compare Premium SSD v1 and v2 using Azure
Database for MySQL Flexible Server 8.4. The same assets are also intended for ongoing monitoring of
services using Azure MySQL.

The first implementation proved the Azure path end to end but remained benchmark-shaped:

- one collector process was configured for one server through environment variables;
- numeric telemetry was flattened to a `Metric` string and `Value`;
- the curated status allow-list omitted many general diagnostic measurements;
- Grafana contained three broad dashboards;
- raw metrics expired after 30 days while rollups remained for 395 days.

Expanding only the allow-list would make the collector look broader while leaking schema, unit,
dimension and scheduling rules into Python, ADX, KQL and dashboard JSON.

## Decision

The product is a **general-purpose monitoring system for Azure Database for MySQL Flexible Server
8.4**. Production monitoring is the primary mode. Benchmarking is a comparison mode over the same
telemetry.

The supported topology is:

```text
multiple Azure MySQL Targets -> one collector VM -> ADX -> Grafana
```

The collector uses a Telegraf-inspired Telemetry Point contract:

- `measurement` identifies a stable measurement family;
- `tags` hold dimensions;
- `fields` hold related values;
- catalog metadata defines kind, unit, cardinality and derived semantics.

A repository-owned Metric Catalog is the source of truth. A YAML configuration declares Targets
and Profiles, and compiles into a validated Collection Plan. Literal credentials are rejected;
configuration may only reference environment variables or Azure Key Vault secrets.

High-cardinality Collection Groups are opt-in and bounded. Different groups have independent
cadences so expensive schema, table, index and digest queries do not run at the global-status
interval.

All ADX telemetry, including raw data, events, materialized views and rollups, has a strict
**90-day retention**.

Grafana dashboards are organized by operator questions. The Premium SSD comparison view displays
QPS, IOPS, throughput, file IO latency and query latency from the same measurements used for
production monitoring.

## Consequences

### Positive

- One collector VM can monitor a fleet of Azure MySQL Targets.
- New Collection Groups use one telemetry contract instead of inventing row shapes.
- Counter/gauge semantics and units remain consistent from collection through visualization.
- Operators can select safe monitoring depth without editing Python.
- Benchmark results remain directly comparable to production telemetry.
- The 90-day lifecycle is unambiguous and enforceable.

### Negative

- The current single-target environment-variable interface needs a compatibility period.
- The current `MysqlMetrics` schema and dashboards need migration adapters.
- YAML adds one core runtime dependency.
- Packed fields are easy to evolve but not ideal for every Grafana query; dashboard-critical
  series require an ADX projection optimized for time-series access.
- High-cardinality coverage must remain bounded, so this is not an unrestricted query profiler.

## Rejected alternatives

### Add every `SHOW GLOBAL STATUS` value to the existing table

Rejected because it increases volume without adding file IO latency, statement dimensions,
schema/table information or collection-cost controls. It also leaves semantics encoded in strings.

### Create one ADX table for every measurement family

Rejected as the default contract because the public schema grows with every Collection Group and
forces coordinated migrations. Specialized projections may still be used where query locality
justifies them.

### Keep benchmark and production collectors separate

Rejected because QPS, IO and latency have the same meaning in both modes. Separate collectors
would duplicate implementation and allow the definitions to drift.

### Retain rollups beyond 90 days

Rejected because the requested lifecycle applies to all telemetry, not only raw data.

