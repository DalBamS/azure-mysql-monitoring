# azure-native/alerts/ — Alert rules

Alert rule definitions and thresholds for Azure Database for MySQL Flexible Server (MySQL 8.4),
covering production monitoring for gaming customers.

## Expected contents

| File | Purpose |
|---|---|
| `thresholds.md` | Documented thresholds and the reasoning behind each |
| `cpu-high.alert.json` | Sustained CPU saturation |
| `storage-near-full.alert.json` | Storage capacity approaching limit |
| `connections-exhausted.alert.json` | `max_connections` pressure / aborted connects |
| `replica-lag.alert.json` | Replication lag |
| `slow-query-spike.alert.json` | Log-based alert on slow query rate |

Rules are authored here and **deployed via Bicep** from [`../bicep/`](../bicep/) — this directory
holds the definitions and documentation, not portal-created resources.

## Conventions

- Log-based alerts reuse the queries in [`../kql/`](../kql/); keep the two in sync.
- Alert evaluation windows and timestamps are expressed in **UTC**.
- Every rule documents: signal, threshold, evaluation window, severity, and action group.
- Action groups (email, webhook, on-call integration) are parameterised — never commit real
  endpoints, phone numbers, or webhook secrets.

## Benchmark vs production

Alerts are aimed at **ongoing production monitoring**. During benchmark runs, load is intentionally
extreme, so either suppress these rules or scope them away from benchmark servers to avoid noise.
Benchmark analysis is driven by `mysql-internal/` and `benchmark-integration/`, not by alerts.

## Configuration

Alert rules run inside Azure and need no database credentials. Any supporting tooling reads only
environment variables — nothing is hardcoded:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN |
| `MYSQL_USER` | MySQL user |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Default database/schema |
| `RUN_ID` | Benchmark run identifier |

## Premium SSD v2 caveat

Premium SSD v2 is in **preview**. Metrics backing some rules may be unavailable on v2-backed
servers, so an alert can stay silent because data is missing rather than because the server is
healthy. Pair storage alerts with collector-side checks.
