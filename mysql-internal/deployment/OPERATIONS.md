# Collector VM operations

## Normal checks

```bash
sudo systemctl status azure-mysql-monitoring.service
sudo journalctl -u azure-mysql-monitoring.service --since "15 minutes ago"
sudo /path/to/repo/mysql-internal/deployment/scripts/health-check.sh
```

The local check proves the process and recovery path are healthy. Grafana **Collector Health**
proves end-to-end delivery: each Target must have a fresh heartbeat and `mysql_reachable=1`.

## Outage behavior

| Failure | Expected behavior | Operator action |
|---|---|---|
| One MySQL Target unavailable | Its worker backs off; sibling Targets continue; heartbeat reports unreachable | Check DNS, Flexible Server state, firewall/private route and TLS |
| ADX streaming unavailable | Exact table projections remain as per-Target `.ready.jsonl` segments | Restore ADX/network; queued replay runs automatically |
| Key Vault denied | Target cannot resolve credentials and backs off independently | Confirm VM principal has Key Vault Secrets User and the referenced secret exists |
| Spool reaches 80% | `health-check.sh` warns | Restore ADX or increase the disk budget after capacity review |
| Spool reaches 100% | Existing data is retained; newest batches are dropped with CRITICAL journal entries | Restore ingestion immediately; do not delete pending segments |
| Corrupt/incomplete segment | File is retained with `.corrupt`; replay continues past it on the next scan | Copy it for analysis, repair only with a reviewed script, then replay separately |
| Terminal queued-ingestion failure | Segment is retained as `.failed.jsonl` | Fix schema/mapping/authorization, then rename to `.ready.jsonl` |

The spool writes and `fsync`s each table batch before streaming ingestion. A process crash can
therefore leave a `.tmp`; startup validates it and either promotes it to `.ready.jsonl` or
quarantines it as `.corrupt`. Successful streaming segments are deleted immediately. Failed segments are submitted unchanged
through ADX queued ingestion and retained until ADX reports terminal success. If the success status
is lost, the collector resubmits with the same `ingest-if-not-exists` tag.

Queued replay uses a content-derived `ingest-by` tag and `ingest-if-not-exists`. If a terminal status
is lost, resubmission with the same tag does not create a second extent. Streaming ingestion still
has normal transport ambiguity, so dashboard queries remain tolerant of duplicate observations.

## Recovery

1. Restore network or ADX availability.
2. Watch `journalctl -fu azure-mysql-monitoring.service` for queued replay.
3. Run `health-check.sh` until pending files and bytes return to zero.
4. Confirm all Targets are fresh in **Collector Health**.
5. If `.overflow` exists, record the loss window and remove only that marker after recovery.
6. Preserve `.corrupt` files and incident timestamps; never bulk-delete the spool.

Cursor files under `/var/lib/azure-mysql-monitoring/cursors` are separate from the spool. Back them
up with the VM disk. Losing a cursor can duplicate or skip `performance_schema.error_log` entries
because that source is a ring buffer.

## Reboot and service recovery

```bash
sudo reboot
# after reconnecting through Bastion/private access:
systemctl is-enabled azure-mysql-monitoring.service
sudo systemctl status azure-mysql-monitoring.service
```

The service is enabled at boot, waits for `network-online.target`, preserves cursor/spool state, and
retries each Target independently.

## Upgrade

```bash
git fetch --all --prune
git checkout <reviewed-release>
sudo mysql-internal/deployment/scripts/install.sh
/opt/azure-mysql-monitoring/venv/bin/python \
  /opt/azure-mysql-monitoring/collector/plan.py \
  /etc/azure-mysql-monitoring/monitoring.yaml
sudo systemctl restart azure-mysql-monitoring.service
```

The installer preserves `/etc/azure-mysql-monitoring` and `/var/lib/azure-mysql-monitoring`.
Validate Grafana freshness before ending the change window.

## Rollback

Checkout the previous reviewed release, run `install.sh` again, and restart the service. Do not roll
back the Collection Plan separately from collector code when it uses groups added by the new
version. ADX schema changes must remain backward-compatible during the rollback window.
