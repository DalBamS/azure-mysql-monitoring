#!/usr/bin/env python3
"""Prepare and run a deterministic open-loop MySQL storage workload."""

from __future__ import annotations

import argparse
import json
import logging
import math
import queue
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

COLLECTOR_DIR = Path(__file__).resolve().parents[1] / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR_DIR))

from config import Config, ConfigError  # noqa: E402
from connection import connect  # noqa: E402

log = logging.getLogger("open-loop-workload")

MIB = 1024 * 1024
TABLE = "benchmark_open_loop"
DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id          BIGINT PRIMARY KEY,
    payload     BLOB NOT NULL,
    version     BIGINT NOT NULL DEFAULT 0,
    updated_at  DATETIME(6) NOT NULL
) ENGINE=InnoDB
"""
SENTINEL = object()


@dataclass(frozen=True, slots=True)
class Operation:
    scheduled_at: float
    kind: str
    row_id: int


@dataclass(slots=True)
class RunStats:
    scheduled: int = 0
    completed_reads: int = 0
    completed_writes: int = 0
    dropped_queue: int = 0
    dropped_late: int = 0
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    failure_messages: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_completion(self, kind: str, latency_ms: float) -> None:
        with self.lock:
            if kind == "read":
                self.completed_reads += 1
            else:
                self.completed_writes += 1
            self.latencies_ms.append(latency_ms)

    def record_failure(self, message: str) -> None:
        with self.lock:
            self.errors += 1
            self.failure_messages.append(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def operation_stream(
    *,
    seed: int,
    count: int,
    row_count: int,
    read_percent: int,
    started_at: float,
    rate: int,
) -> Iterator[Operation]:
    rng = random.Random(seed)
    for sequence in range(count):
        yield Operation(
            scheduled_at=started_at + sequence / rate,
            kind="read" if rng.randrange(100) < read_percent else "write",
            row_id=rng.randint(1, row_count),
        )


def write_result(path: Path | None, result: dict[str, Any]) -> None:
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def table_inventory(conn: Any) -> dict[str, int]:
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*), COALESCE(MAX(id), 0) FROM {TABLE}")
        row_count, max_id = cursor.fetchone()
        cursor.execute(
            """
            SELECT COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0)
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (TABLE,),
        )
        size_row = cursor.fetchone()
        cursor.execute("SELECT @@GLOBAL.innodb_buffer_pool_size")
        buffer_pool_bytes = cursor.fetchone()[0]
    return {
        "rows": int(row_count),
        "max_id": int(max_id),
        "table_bytes": int(size_row[0] if size_row else 0),
        "buffer_pool_bytes": int(buffer_pool_bytes),
    }


def prepare_dataset(cfg: Config, args: argparse.Namespace) -> int:
    if args.dataset_mib < 128 or not 1024 <= args.payload_bytes <= 65535:
        log.error("dataset-mib must be >= 128 and payload-bytes must be 1024-65535")
        return 2

    target_rows = math.ceil(args.dataset_mib * MIB / args.payload_bytes)
    payload = random.Random(args.seed).randbytes(args.payload_bytes)
    conn = connect(cfg)
    try:
        with conn.cursor() as cursor:
            cursor.execute(DDL)
        inventory = table_inventory(conn)
        next_id = inventory["max_id"] + 1
        remaining = max(0, target_rows - inventory["rows"])
        log.info(
            "preparing %s MiB: target=%d rows, existing=%d rows",
            args.dataset_mib,
            target_rows,
            inventory["rows"],
        )

        inserted = 0
        while inserted < remaining:
            batch_count = min(args.batch_size, remaining - inserted)
            rows = [
                (next_id + offset, payload)
                for offset in range(batch_count)
            ]
            with conn.cursor() as cursor:
                cursor.executemany(
                    f"""
                    INSERT INTO {TABLE} (id, payload, version, updated_at)
                    VALUES (%s, %s, 0, UTC_TIMESTAMP(6))
                    """,
                    rows,
                )
            inserted += batch_count
            next_id += batch_count
            if inserted % max(args.batch_size, 4096) == 0 or inserted == remaining:
                log.info("inserted %d/%d rows", inserted, remaining)

        with conn.cursor() as cursor:
            cursor.execute(f"ANALYZE TABLE {TABLE}")
            cursor.fetchall()
        inventory = table_inventory(conn)
        result = {
            "command": "prepare",
            "completed_at": utc_now(),
            "target": cfg.host,
            "tier": cfg.tier,
            "requested_dataset_mib": args.dataset_mib,
            **inventory,
        }
        write_result(args.result, result)
        if inventory["rows"] < target_rows:
            log.error("dataset preparation ended below the requested row count")
            return 3
        if inventory["table_bytes"] <= inventory["buffer_pool_bytes"]:
            log.error(
                "table (%d bytes) is not larger than buffer pool (%d bytes)",
                inventory["table_bytes"],
                inventory["buffer_pool_bytes"],
            )
            return 4
        return 0
    finally:
        conn.close()


def _worker(
    worker_id: int,
    conn: Any,
    work_queue: queue.Queue[object],
    stats: RunStats,
    max_lag_seconds: float,
) -> None:
    try:
        with conn.cursor() as cursor:
            while True:
                item = work_queue.get()
                try:
                    if item is SENTINEL:
                        return
                    assert isinstance(item, Operation)
                    started = time.perf_counter()
                    if started - item.scheduled_at > max_lag_seconds:
                        with stats.lock:
                            stats.dropped_late += 1
                        continue
                    if item.kind == "read":
                        cursor.execute(
                            f"SELECT OCTET_LENGTH(payload), version FROM {TABLE} WHERE id = %s",
                            (item.row_id,),
                        )
                        cursor.fetchone()
                    else:
                        cursor.execute(
                            f"""
                            UPDATE {TABLE}
                            SET version = version + 1, updated_at = UTC_TIMESTAMP(6)
                            WHERE id = %s
                            """,
                            (item.row_id,),
                        )
                    stats.record_completion(
                        item.kind,
                        (time.perf_counter() - item.scheduled_at) * 1000.0,
                    )
                except Exception as exc:  # noqa: BLE001
                    stats.record_failure(f"worker {worker_id}: {exc}")
                    return
                finally:
                    work_queue.task_done()
    finally:
        conn.close()


def run_open_loop(cfg: Config, args: argparse.Namespace) -> int:
    if (
        args.seconds < 30
        or args.threads < 1
        or args.rate < 1
        or not 0 <= args.read_percent <= 100
        or args.max_lag_seconds <= 0
    ):
        log.error("invalid run arguments")
        return 2

    inspection = connect(cfg)
    try:
        inventory = table_inventory(inspection)
    finally:
        inspection.close()
    if inventory["rows"] < 1:
        log.error("dataset is empty; run the prepare command first")
        return 3
    if inventory["table_bytes"] <= inventory["buffer_pool_bytes"]:
        log.error(
            "table (%d bytes) must exceed buffer pool (%d bytes)",
            inventory["table_bytes"],
            inventory["buffer_pool_bytes"],
        )
        return 3

    connections = []
    try:
        connections = [connect(cfg) for _ in range(args.threads)]
    except Exception as exc:  # noqa: BLE001
        for conn in connections:
            conn.close()
        log.error("worker connection setup failed: %s", exc)
        return 4

    queue_capacity = max(args.threads, math.ceil(args.rate * args.max_lag_seconds))
    work_queue: queue.Queue[object] = queue.Queue(maxsize=queue_capacity)
    stats = RunStats()
    workers = [
        threading.Thread(
            target=_worker,
            args=(worker_id, conn, work_queue, stats, args.max_lag_seconds),
            daemon=True,
        )
        for worker_id, conn in enumerate(connections)
    ]
    for worker in workers:
        worker.start()

    operation_count = args.seconds * args.rate
    started_at = time.perf_counter() + 1.0
    started_utc = utc_now()
    for operation in operation_stream(
        seed=args.seed,
        count=operation_count,
        row_count=inventory["max_id"],
        read_percent=args.read_percent,
        started_at=started_at,
        rate=args.rate,
    ):
        sleep_for = operation.scheduled_at - time.perf_counter()
        if sleep_for > 0:
            time.sleep(sleep_for)
        with stats.lock:
            stats.scheduled += 1
        try:
            work_queue.put_nowait(operation)
        except queue.Full:
            with stats.lock:
                stats.dropped_queue += 1

    for _ in workers:
        while True:
            try:
                work_queue.put(SENTINEL, timeout=1)
                break
            except queue.Full:
                if not any(worker.is_alive() for worker in workers):
                    break
    for worker in workers:
        worker.join(timeout=args.max_lag_seconds + 30)

    alive = [index for index, worker in enumerate(workers) if worker.is_alive()]
    if alive:
        stats.record_failure(f"workers did not stop: {alive}")

    completed = stats.completed_reads + stats.completed_writes
    dropped = stats.dropped_queue + stats.dropped_late
    result = {
        "command": "run",
        "started_at": started_utc,
        "completed_at": utc_now(),
        "run_id": cfg.run_id,
        "target": cfg.host,
        "tier": cfg.tier,
        "seconds": args.seconds,
        "threads": args.threads,
        "offered_rate_ops_s": args.rate,
        "read_percent": args.read_percent,
        "scheduled": stats.scheduled,
        "completed": completed,
        "completed_reads": stats.completed_reads,
        "completed_writes": stats.completed_writes,
        "dropped": dropped,
        "dropped_queue": stats.dropped_queue,
        "dropped_late": stats.dropped_late,
        "errors": stats.errors,
        "achieved_rate_ops_s": round(completed / args.seconds, 3),
        "completion_percent": round(100.0 * completed / max(1, stats.scheduled), 3),
        "latency_p50_ms": round(percentile(stats.latencies_ms, 0.50), 3),
        "latency_p95_ms": round(percentile(stats.latencies_ms, 0.95), 3),
        "latency_p99_ms": round(percentile(stats.latencies_ms, 0.99), 3),
        "failure_messages": stats.failure_messages[:20],
        **inventory,
    }
    write_result(args.result, result)
    return 4 if stats.errors or alive else 0


def cleanup_dataset(cfg: Config, args: argparse.Namespace) -> int:
    conn = connect(cfg)
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {TABLE}")
        write_result(
            args.result,
            {
                "command": "cleanup",
                "completed_at": utc_now(),
                "target": cfg.host,
                "tier": cfg.tier,
            },
        )
        return 0
    finally:
        conn.close()


def reset_statement_digests(cfg: Config, args: argparse.Namespace) -> int:
    conn = connect(cfg)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "TRUNCATE TABLE performance_schema.events_statements_summary_by_digest"
            )
        write_result(
            args.result,
            {
                "command": "reset-digests",
                "completed_at": utc_now(),
                "target": cfg.host,
                "tier": cfg.tier,
            },
        )
        return 0
    finally:
        conn.close()


def wait_for_buffer_pool(cfg: Config, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout_seconds
    last_size = 0
    last_status = ""
    while time.monotonic() < deadline:
        conn = connect(cfg)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT @@GLOBAL.innodb_buffer_pool_size")
                last_size = int(cursor.fetchone()[0])
                cursor.execute(
                    "SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_resize_status'"
                )
                row = cursor.fetchone()
                last_status = str(row[1] if row else "")
        finally:
            conn.close()
        log.info(
            "buffer pool runtime=%d expected=%d status=%s",
            last_size,
            args.expected_bytes,
            last_status,
        )
        normalized_status = last_status.lower()
        resize_finished = (
            not normalized_status
            or normalized_status.startswith("completed")
            or "nothing to do" in normalized_status
            or "size did not change" in normalized_status
        )
        if last_size == args.expected_bytes and resize_finished:
            return 0
        if normalized_status.startswith("failed"):
            return 4
        time.sleep(args.poll_seconds)
    log.error(
        "buffer pool did not reach %d bytes; runtime=%d status=%s",
        args.expected_bytes,
        last_size,
        last_status,
    )
    return 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--dataset-mib", type=int, default=2048)
    prepare.add_argument("--payload-bytes", type=int, default=16 * 1024)
    prepare.add_argument("--batch-size", type=int, default=64)
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument("--result", type=Path)

    run = subparsers.add_parser("run")
    run.add_argument("--seconds", type=int, default=120)
    run.add_argument("--threads", type=int, default=16)
    run.add_argument("--rate", type=int, default=500)
    run.add_argument("--read-percent", type=int, default=80)
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--max-lag-seconds", type=float, default=2.0)
    run.add_argument("--result", type=Path, required=True)

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--result", type=Path)
    reset_digests = subparsers.add_parser("reset-digests")
    reset_digests.add_argument("--result", type=Path)
    wait_buffer_pool = subparsers.add_parser("wait-buffer-pool")
    wait_buffer_pool.add_argument("--expected-bytes", type=int, required=True)
    wait_buffer_pool.add_argument("--timeout-seconds", type=int, default=600)
    wait_buffer_pool.add_argument("--poll-seconds", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    args = parse_args(argv)
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.command == "prepare":
        return prepare_dataset(cfg, args)
    if args.command == "run":
        return run_open_loop(cfg, args)
    if args.command == "cleanup":
        return cleanup_dataset(cfg, args)
    if args.command == "reset-digests":
        return reset_statement_digests(cfg, args)
    return wait_for_buffer_pool(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
