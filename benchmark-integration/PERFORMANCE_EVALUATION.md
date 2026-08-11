# Performance evaluation and reporting

Use the Grafana benchmark dashboard to investigate a comparison interactively, then generate a
source-controlled Markdown report from the same ADX functions. The dashboard and report must select
the same four identities:

- baseline `RUN_ID`;
- baseline Target;
- candidate `RUN_ID`;
- candidate Target.

## 1. Make runs comparable

Keep these fixed across both runs:

| Dimension | Rule |
|---|---|
| MySQL | Azure Database for MySQL Flexible Server 8.4 |
| Server | Same compute SKU, vCores, memory, parameters, HA and region |
| Data | Same data snapshot, cardinality and cache warm-up |
| Workload | Same client count, query mix, ramp-up and measured duration |
| Collector | Same Metric Catalog, Profile and collection cadence |
| Time | UTC timestamps; exclude setup, warm-up and teardown |
| Variable under test | Change only the intended factor, such as Premium SSD v1 → v2 |

Use at least three repetitions per configuration. A single pair is useful for diagnosis but is not
enough to establish a stable performance difference.

## 2. Review the Grafana dashboard

Open **Benchmark — Premium SSD v1 vs v2** and select both Run/Target pairs.

Review in this order:

1. **Run inventory** — durations and sample counts must be similar.
2. **Headline comparison** — compare p95 before Avg or Max.
3. **Delta** — positive change is not always good.
4. **Read IOPS overlay** — confirm both runs execute the same workload phases.
5. **File IO latency overlay** — identify sustained or phase-specific latency.
6. **Query Performance dashboard** — correlate IO changes with query p95/p99 and no-index usage.
7. **Storage IO dashboard** — identify read/write mode and file class causing the difference.
8. **Production Overview** — rule out connection, thread, buffer-pool or error anomalies.

Interpretation direction:

| Metric | Better direction | Use |
|---|---|---|
| QPS | Higher | Delivered workload |
| Read/write IOPS | Higher at equal workload/latency | Storage work completed |
| Read/write throughput | Higher at equal workload/latency | Storage bytes completed |
| Read/write latency | Lower | Engine-observed storage wait per operation |
| Query p95/p99 | Lower | Gaming-service tail latency |
| Query errors | Lower | Statement execution failures |
| Redo log waits | Lower | Write-path pressure |

Never average per-file latency values. The repository computes operation-weighted latency:

$$
\text{latency}_{ms/op} =
\frac{\Delta \text{wait\_ms\_total}}{\Delta \text{operations\_total}}
$$

## 3. Generate the report

Install the collector ADX extras and load the normal environment:

```bash
pip install -r mysql-internal/collector/requirements-adx.txt
export ADX_CLUSTER_URI="https://<cluster>.<region>.kusto.windows.net"
export ADX_DATABASE="mysqlmonitoring"
export GRAFANA_ENDPOINT="https://<workspace>.<region>.grafana.azure.com"

python benchmark-integration/performance_report.py \
  --baseline-run ssdv1-2026-08-10-01 \
  --baseline-target orders-v1 \
  --candidate-run ssdv2-2026-08-10-01 \
  --candidate-target orders-v2 \
  --from-utc 2026-08-10T00:00:00Z \
  --to-utc 2026-08-11T00:00:00Z
```

The report is written under `benchmark-integration/report/` unless `--output` is supplied. It
contains:

- a deep link that reopens the exact Grafana selection and UTC range;
- run comparability and data-quality gates;
- p95 baseline/candidate values and percent changes;
- direction-aware improvement/regression labels;
- an overall `PASS`, `REGRESSION`, or `INCONCLUSIVE` result;
- a decision-record section for reviewer sign-off.

The default material-change threshold is 5%. Change it only before looking at results:

```bash
python benchmark-integration/performance_report.py ... \
  --material-change-pct 10 \
  --max-duration-difference-pct 5 \
  --max-sample-difference-pct 10
```

## 4. Analyse anomalies

| Pattern | Likely interpretation | Next check |
|---|---|---|
| QPS up, latency unchanged/down | Candidate improvement | Confirm equal client pressure and errors |
| IOPS up, QPS unchanged | More IO per query, not necessarily better | Buffer-pool hit ratio, query plans, table/index IO |
| File latency down, query p99 unchanged | Bottleneck is above storage | Locks, CPU, statement digest p99 |
| Avg improves, p95/p99 regresses | Tail regression | Workload phases and top digests |
| Samples/duration differ | Invalid comparison | Rerun with identical window |
| Azure Monitor disagrees with collector | Preview metric gap or aggregation difference | Treat collector as authoritative; document discrepancy |

## 5. Preserve evidence

- Commit the Markdown report only when it contains no customer-identifying workload text.
- Keep raw benchmark JSONL outside Git; it may contain customer-shaped details.
- Record workload version, server configuration, data snapshot and anomalies in the report.
- ADX telemetry expires after exactly 90 days. Preserve approved conclusions in the report before
  the source window expires.
- A dashboard screenshot is supporting evidence, not the source of truth. The Run/Target IDs and UTC
  window make the result reproducible.
