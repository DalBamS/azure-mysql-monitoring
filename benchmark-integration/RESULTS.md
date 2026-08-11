# Premium SSD v1 vs Premium SSD v2 benchmark result

## Decision

**Premium SSD v2 is a promising performance improvement, but this run is not sufficient for an
unconditional production approval.** Across all three repetitions it improved QPS, write IOPS,
write throughput, and query p99 latency. However, reported write latency increased and the v2
server repeatedly recorded a failing replica-status statement. The two storage tiers also require
different compute generations, so the observed delta cannot be attributed to storage alone.

## Test configuration

| Setting | Baseline | Candidate |
|---|---|---|
| Server | `mysqlbm-euson-v1` | `mysqlbm-euson-v2` |
| MySQL | 8.4.9-azure | 8.4.9-azure |
| Compute | Standard_E8ds_v5 (8 vCores, 64 GiB) | Standard_E8ds_v6 (8 vCores, 64 GiB) |
| Storage | Premium SSD v1, 64 GiB | Premium SSD v2, 64 GiB |
| Provisioned storage performance | 492 IOPS | 5,000 IOPS / 125 MBps |
| HA | Disabled | Disabled |
| Security | TLS enforced | TLS enforced |

- **Batch:** `storage-final-20260811-a7f9`
- **Region:** Korea Central
- **Workload:** 8 closed-loop workers, 120 seconds, deterministic per-worker seed
- **Collection:** 5-30 second group intervals, 171-174 seconds per Run/Target pair
- **Authoritative data:** `mysql-internal` JSONL uploaded to ADX; Azure Monitor is supplementary
  while Premium SSD v2 remains in preview

## Three-run result

| Metric | v1 median | v2 median | Per-run candidate change | Interpretation |
|---|---:|---:|---:|---|
| QPS (p95) | 261.976 | 297.303 | +10.7% to +13.8% | Consistent improvement |
| Write IOPS (p95) | 453.233 | 870.167 | +77.0% to +110.4% | Consistent improvement |
| Write throughput MiB/s (p95) | 3.209 | 7.145 | +103.9% to +142.7% | Consistent improvement |
| Write latency ms (p95) | 0.046 | 0.056 | +21.7% to +48.3% | Consistent regression |
| Query p95 ms | 724.436 | 724.436 | 0.0% | No material change |
| Query p99 ms | 912.011 | 831.764 | -8.8% | Consistent improvement |
| Redo log waits/s (p95) | 0.000 | 0.000 | 0.0% | No pressure observed |
| Read IOPS (p95) | 0.000 | 0.000 | 0.0% | Workload was write-oriented |

The candidate completed a median 214,975 inserts versus 192,625 for the baseline during the same
120-second window. All six workloads retained all eight workers until completion. No workload log
contained a deadlock, worker stop, or error.

## Anomaly requiring follow-up

ADX reported candidate statement errors at 0.037-0.061 errors/s while the workload processes
reported no errors. The changing performance-schema digest was identified as:

```text
SHOW REPLICA STATUS FOR CHANNEL ?
```

It accumulated five or six errors per repetition on the Premium SSD v2 server and none on the
baseline. This is not one of the synthetic workload statements. It is a server/service-side probe
or preview behavior, but it remains a real candidate-only anomaly and is why the automated
per-repetition reports return `REGRESSION`.

## Interpretation limits

1. Premium SSD v2 requires v6 compute, while Premium SSD v1 is rejected on v6. The closest valid
   pair therefore compares E8ds v5 with E8ds v6; CPU-generation effects are inseparable here.
2. The working set was mostly served from memory and the workload was write-oriented, so read IOPS
   and read throughput remained zero.
3. The closed-loop workload lets a faster target complete more operations. This reflects delivered
   throughput but means the two servers do not process an identical operation count.
4. TLS encryption was enforced, but this test environment did not configure `MYSQL_SSL_CA` for
   pinned CA verification. Production collection must set the CA bundle.

## Recommendation

Proceed with a longer, open-loop validation that fixes the offered request rate, includes a
read-heavy data set larger than the buffer pool, and investigates the candidate-only replica-status
errors. Treat Premium SSD v2 as the preferred performance candidate only after that anomaly is
resolved or accepted and the test is repeated with production-representative latency objectives.

