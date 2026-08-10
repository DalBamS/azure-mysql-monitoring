#!/usr/bin/env python3
"""Generate database load so the monitoring pipeline has something real to observe.

Without load every counter stays flat, and a flat chart is indistinguishable from a broken
collector — exactly the false negative this test environment exists to rule out. This script
moves the specific counters the dashboards plot:

    Innodb_data_reads / _writes    physical I/O (the SSD v1 vs v2 signal)
    Innodb_rows_inserted/_read     row activity
    Com_select / Com_insert        statement throughput
    Created_tmp_disk_tables        a deliberately bad query spilling to disk
    Slow_queries                   with long_query_time=0, feeds MySqlSlowLogs

    python workload.py --seconds 180
    python workload.py --seconds 60 --threads 4

Configuration comes from the standard environment variables. Run `. ./scripts/load-env.ps1`
first.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import string
import sys
import threading
import time
from pathlib import Path

COLLECTOR_DIR = Path(__file__).resolve().parents[2] / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR_DIR))

from config import Config, ConfigError  # noqa: E402
from connection import connect  # noqa: E402

log = logging.getLogger("workload")

DDL = """
CREATE TABLE IF NOT EXISTS monitoring_load (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id      VARCHAR(64)  NOT NULL,
    bucket      INT          NOT NULL,
    payload     VARCHAR(255) NOT NULL,
    created_at  DATETIME(6)  NOT NULL,
    INDEX idx_bucket (bucket)
) ENGINE=InnoDB
"""

_stop = threading.Event()


def _random_payload(size: int = 200) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=size))


def worker(cfg: Config, worker_id: int, stats: dict) -> None:
    """One connection running a mixed read/write workload until stopped."""
    try:
        conn = connect(cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("worker %d could not connect: %s", worker_id, exc)
        return

    local = {"inserts": 0, "selects": 0, "updates": 0, "heavy": 0}

    try:
        with conn.cursor() as cur:
            while not _stop.is_set():
                # Writes: dirty pages, redo log traffic, eventual flushes to storage.
                rows = [
                    (cfg.run_id, random.randint(1, 50), _random_payload())
                    for _ in range(25)
                ]
                cur.executemany(
                    "INSERT INTO monitoring_load (run_id, bucket, payload, created_at) "
                    "VALUES (%s, %s, %s, UTC_TIMESTAMP(6))",
                    rows,
                )
                local["inserts"] += len(rows)

                # Indexed reads: mostly buffer-pool hits.
                cur.execute(
                    "SELECT COUNT(*), MAX(id) FROM monitoring_load WHERE bucket = %s",
                    (random.randint(1, 50),),
                )
                cur.fetchall()
                local["selects"] += 1

                # Updates: row locks and dirty-page churn.
                cur.execute(
                    "UPDATE monitoring_load SET payload = %s "
                    "WHERE bucket = %s ORDER BY id DESC LIMIT 10",
                    (_random_payload(), random.randint(1, 50)),
                )
                local["updates"] += 1

                # A deliberately bad query, roughly every tenth iteration: no usable index,
                # forces a temporary table, and with long_query_time=0 it lands in the slow
                # query log. This is what gives Layer 1 something to show.
                if random.random() < 0.1:
                    cur.execute(
                        "SELECT bucket, COUNT(*) c, AVG(LENGTH(payload)) a "
                        "FROM monitoring_load "
                        "WHERE payload LIKE %s "
                        "GROUP BY bucket ORDER BY c DESC",
                        (f"%{_random_payload(3)}%",),
                    )
                    cur.fetchall()
                    local["heavy"] += 1

                time.sleep(0.05)
    except Exception as exc:  # noqa: BLE001
        log.error("worker %d stopped: %s", worker_id, exc)
    finally:
        conn.close()
        for key, value in local.items():
            stats[key] = stats.get(key, 0) + value


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate MySQL load for monitoring verification")
    parser.add_argument("--seconds", type=int, default=180, help="How long to run")
    parser.add_argument("--threads", type=int, default=2, help="Concurrent connections")
    parser.add_argument("--keep-table", action="store_true", help="Do not drop the load table")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    log.info("target %s/%s as %s", cfg.host, cfg.database, cfg.user)

    try:
        setup = connect(cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("could not connect: %s", exc)
        return 3

    with setup.cursor() as cur:
        cur.execute(DDL)
    setup.close()
    log.info("load table ready")

    stats: dict = {}
    threads = [
        threading.Thread(target=worker, args=(cfg, i, stats), daemon=True)
        for i in range(args.threads)
    ]

    log.info("running %d workers for %ds", args.threads, args.seconds)
    started = time.monotonic()
    for t in threads:
        t.start()

    try:
        while time.monotonic() - started < args.seconds:
            time.sleep(1)
            elapsed = int(time.monotonic() - started)
            if elapsed % 30 == 0:
                log.info("%ds elapsed", elapsed)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        _stop.set()
        for t in threads:
            t.join(timeout=10)

    log.info("done: %s", stats or "no work recorded")

    if not args.keep_table:
        try:
            cleanup = connect(cfg)
            with cleanup.cursor() as cur:
                # DROP rather than DELETE: it releases the storage, and the counters this
                # generated are already recorded in ADX where the verification reads them.
                cur.execute("DROP TABLE IF EXISTS monitoring_load")
            cleanup.close()
            log.info("load table dropped")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not drop the load table: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
