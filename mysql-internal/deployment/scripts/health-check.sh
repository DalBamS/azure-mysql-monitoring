#!/usr/bin/env bash
set -euo pipefail

env_file="${COLLECTOR_ENV_FILE:-/etc/azure-mysql-monitoring/collector.env}"
if [[ -r "$env_file" ]]; then
  set -a
  # The file is root-owned and contains settings/references only.
  source "$env_file"
  set +a
fi

state_root="${COLLECTOR_STATE_DIR:-/var/lib/azure-mysql-monitoring}"
spool_dir="${COLLECTOR_SPOOL_DIR:-$state_root/spool}"
max_bytes="${COLLECTOR_SPOOL_MAX_BYTES:-1073741824}"

systemctl is-active --quiet azure-mysql-monitoring.service || {
  echo "CRITICAL: azure-mysql-monitoring.service is not active"
  exit 2
}

pending_files="$(find "$spool_dir" -type f -name '*.ready.jsonl' -print 2>/dev/null | wc -l)"
corrupt_files="$(find "$spool_dir" -type f -name '*.corrupt' -print 2>/dev/null | wc -l)"
failed_files="$(find "$spool_dir" -type f -name '*.failed.jsonl' -print 2>/dev/null | wc -l)"
pending_bytes="$(du -sb "$spool_dir" 2>/dev/null | awk '{print $1}')"
pending_bytes="${pending_bytes:-0}"

if [[ -e "$spool_dir/.overflow" ]]; then
  echo "CRITICAL: spool rejected telemetry; inspect $spool_dir/.overflow and clear it after recovery"
  exit 2
fi
if (( corrupt_files > 0 )); then
  echo "CRITICAL: $corrupt_files corrupt spool segment(s) require operator review"
  exit 2
fi
if (( failed_files > 0 )); then
  echo "CRITICAL: $failed_files terminal ADX ingestion failure segment(s) require operator review"
  exit 2
fi
if (( pending_bytes >= max_bytes )); then
  echo "CRITICAL: spool is full ($pending_bytes/$max_bytes bytes); newest telemetry is dropping"
  exit 2
fi
if (( pending_bytes * 100 >= max_bytes * 80 )); then
  echo "WARNING: spool is above 80% ($pending_bytes/$max_bytes bytes, $pending_files segments)"
  exit 1
fi

echo "OK: service active; spool=$pending_bytes/$max_bytes bytes, pending=$pending_files"
