# azure-native/workbooks/ — Azure Workbook dashboards

Azure **Workbook** definitions (JSON) that visualise Flexible Server telemetry from the
Log Analytics workspace provisioned in [`../bicep/`](../bicep/).

## Expected contents

| File | Purpose |
|---|---|
| `production-overview.workbook.json` | Day-to-day health view for gaming customer workloads |
| `benchmark-ssd-v1-vs-v2.workbook.json` | Side-by-side Premium SSD v1 vs v2 comparison |
| `storage-io.workbook.json` | IOPS, throughput, latency, storage-limit drill-down |

Workbooks are stored as JSON so they are reviewable in pull requests and deployable via Bicep.
Export from the portal, then commit the JSON — do not treat the portal as the source of truth.

> **Scope note:** [`../../grafana/`](../../grafana/) is the primary operator-facing view, because it
> is the only layer that renders Azure Monitor data and `mysql-internal/` collector data (via
> [`../../adx/`](../../adx/)) on a single time axis. Workbooks remain the Azure-native, portal-side
> view for people already working in the Azure portal, and they keep working even if Grafana is
> unavailable.

## Conventions

- Queries embedded in workbooks should mirror the ones in [`../kql/`](../kql/); keep them in sync.
- All time axes are **UTC ISO-8601**.
- Benchmark workbooks are parameterised by **`RUN_ID`** so a single run can be isolated and
  compared against collector output from `mysql-internal/`.
- MySQL 8.4 only: label redo-log panels using `innodb_redo_log_capacity`, not
  `innodb_log_file_size`.

## Configuration

Workbooks read from Log Analytics and do not connect to MySQL directly. No credentials are ever
embedded in a workbook definition. Repo-wide connection settings, used by the collector and any
supporting tooling, always come from environment variables:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN |
| `MYSQL_USER` | MySQL user |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Default database/schema |
| `RUN_ID` | Benchmark run identifier used as a workbook parameter |

## Premium SSD v2 caveat

v2 is in **preview**; panels driven by Azure platform metrics may show gaps for v2-backed servers.
For benchmark conclusions, cross-check against `mysql-internal/` collector output.
