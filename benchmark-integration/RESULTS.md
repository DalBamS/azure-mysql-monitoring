# Premium SSD v1 vs Premium SSD v2 benchmark result

## Decision

**Prefer Premium SSD v2 for a read-heavy production canary, with an application-latency SLO and
rollback gate.** In the fixed-rate validation, both tiers completed every offered request without
drops or errors, while v2 reduced end-to-end p95 latency by 39.4-42.4% and MySQL query p95 latency
by 63.7-69.8% across all three repetitions.

This is a conditional recommendation rather than proof that storage alone caused the improvement.
Azure currently requires different compute generations for these tiers, Premium SSD v2 remains in
preview, and its measured file write latency was 45.2-80.0% higher despite the better request latency.

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

- **Authoritative batch:** `storage-open-final-20260812-r4`
- **Region:** Korea Central
- **Workload:** fixed 500 operations/s, 80% reads, 16 workers, 120 seconds, three repetitions
- **Operation count:** 60,000 scheduled and completed per Target in every repetition
- **Working set:** 262,144 rows with 16-KiB payloads; approximately 8.02 GiB on disk
- **Buffer pool:** 1 GiB during the benchmark
- **Collection:** 170-174 second windows per Run/Target pair
- **Authoritative data:** `mysql-internal` JSONL uploaded to ADX; Azure Monitor is supplementary
  while Premium SSD v2 remains in preview
- **Grafana comparisons:** [repetition 1](https://mysqlmon-grafana-toqai-c7b0akg4d2c3echa.sel.grafana.azure.com/d/mysqlmon-benchmark-ssd/benchmark-premium-ssd-v1-vs-v2?var-baseline=ssdv1-storage-open-final-20260812-r4-r1&var-baseline_target=mysqlbm-euson-v1&var-candidate=ssdv2-storage-open-final-20260812-r4-r1&var-candidate_target=mysqlbm-euson-v2&from=1786466049525&to=1786466233831),
  [repetition 2](https://mysqlmon-grafana-toqai-c7b0akg4d2c3echa.sel.grafana.azure.com/d/mysqlmon-benchmark-ssd/benchmark-premium-ssd-v1-vs-v2?var-baseline=ssdv1-storage-open-final-20260812-r4-r2&var-baseline_target=mysqlbm-euson-v1&var-candidate=ssdv2-storage-open-final-20260812-r4-r2&var-candidate_target=mysqlbm-euson-v2&from=1786466293351&to=1786466473820),
  [repetition 3](https://mysqlmon-grafana-toqai-c7b0akg4d2c3echa.sel.grafana.azure.com/d/mysqlmon-benchmark-ssd/benchmark-premium-ssd-v1-vs-v2?var-baseline=ssdv1-storage-open-final-20260812-r4-r3&var-baseline_target=mysqlbm-euson-v1&var-candidate=ssdv2-storage-open-final-20260812-r4-r3&var-candidate_target=mysqlbm-euson-v2&from=1786466530709&to=1786466713828)

## Fixed-rate, read-heavy result

| Metric | v1 median | v2 median | Per-run candidate change | Interpretation |
|---|---:|---:|---:|---|
| Completion | 100.0% | 100.0% | 0 drops, 0 errors | Identical delivered load |
| End-to-end p50 ms | 10.789 | 7.242 | -32.9% to -34.7% | Consistent improvement |
| End-to-end p95 ms | 19.140 | 11.464 | -39.4% to -42.4% | Consistent improvement |
| End-to-end p99 ms | 23.687 | 14.727 | -35.3% to -54.7% | Consistent improvement |
| QPS (p95) | 503.404 | 504.002 | +0.0% to +0.2% | Fixed-rate parity |
| Query p95 ms | 19.953 | 6.310 | -63.7% to -69.8% | Consistent improvement |
| Query p99 ms | 20.893 | 6.918 | -66.9% to -86.2% | Consistent improvement |
| Read IOPS (p95) | 896.233 | 895.567 | -0.1% to -0.0% | Identical physical-read demand |
| Read latency ms (p95) | 2.537 | 0.621 | -73.9% to -76.2% | Consistent improvement |
| Write IOPS (p95) | 303.452 | 454.733 | +48.8% to +49.9% | More storage write operations |
| Write throughput MiB/s (p95) | 1.010 | 1.974 | +95.4% to +120.9% | More bytes written to storage |
| Write latency ms (p95) | 0.039 | 0.061 | +45.2% to +80.0% | Consistent regression |
| Query errors/s | 0.000 | 0.000 | 0.0% | No workload errors |
| Redo log waits/s | 0.000 | 0.000 | 0.0% | No pressure observed |

The fixed offered rate removes the closed-loop bias: both Targets executed the same deterministic
operation stream and completed all 60,000 requests per repetition. The larger-than-memory working
set produced approximately 892-897 physical read IOPS on both servers, so the latency result is not
an in-memory-only comparison.

The higher v2 write IOPS and throughput do not represent extra application work because request
counts and seeds were identical. They describe a different storage write pattern. The file write
latency regression is small in absolute terms (v1 0.035-0.042 ms versus v2 0.060-0.063 ms) and did
not reverse the end-to-end latency improvement, but write-heavy production workloads need a separate
canary before adoption.

## Closed-loop supporting result

The earlier batch `storage-final-20260811-a7f9` used eight closed-loop workers for 120 seconds.
Because faster Targets perform more work in a closed loop, it is supporting evidence rather than the
decision run.

| Metric | v1 median | v2 median | Per-run candidate change |
|---|---:|---:|---:|
| QPS (p95) | 261.976 | 297.303 | +10.7% to +13.8% |
| Write IOPS (p95) | 453.233 | 870.167 | +77.0% to +110.4% |
| Write throughput MiB/s (p95) | 3.209 | 7.145 | +103.9% to +142.7% |
| Write latency ms (p95) | 0.046 | 0.056 | +21.7% to +48.3% |
| Query p99 ms | 912.011 | 831.764 | -8.8% |

## Resolved telemetry anomaly

The candidate-only `SHOW REPLICA STATUS FOR CHANNEL ?` failures were reproduced while no collector
or workload was running. They are a schema-less Azure service probe on the Premium SSD v2 preview
server, not a synthetic workload statement. `BenchmarkSummary` now excludes schema-less service
digests from workload query-tail and query-error calculations. The authoritative fixed-rate batch
therefore reports zero workload query errors for both Targets without hiding statements associated
with the benchmark schema.

## Interpretation limits

1. Premium SSD v2 requires v6 compute, while Premium SSD v1 is rejected on v6. The closest valid
   pair compares E8ds v5 with E8ds v6, so CPU-generation effects remain inseparable.
2. The test proves stable operation at 500 operations/s; it does not locate either server's
   saturation point.
3. Premium SSD v2 is a preview feature, so availability, behavior, and Azure Monitor coverage can
   change before general availability.
4. TLS encryption was enforced, but this test environment did not configure `MYSQL_SSL_CA` for
   pinned CA verification. Production collection must set the CA bundle.

## Recommendation

Use Premium SSD v2 for a limited read-heavy gaming workload canary. Gate promotion on application
p95/p99 latency, error rate, and write-heavy transaction behavior, and retain Premium SSD v1 as the
rollback path. Do not generalize this result to write-heavy customers until a production-shaped
write-heavy fixed-rate run confirms the file write-latency tradeoff is acceptable.
