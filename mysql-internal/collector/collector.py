#!/usr/bin/env python3
"""MySQL monitoring collector — entry point.

Polls Azure Database for MySQL Flexible Server (MySQL 8.4) over TLS and emits tagged rows to
one or more sinks.

    python collector.py --interval 5 --sink jsonl --out ../../benchmark-integration/runs/$RUN_ID.jsonl
    python collector.py --interval 10 --sink adx-streaming --sink jsonl

Configuration comes exclusively from environment variables; see README.md.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from config import Config, ConfigError
from connection import assert_tls, connect, server_identity
from envelope import Envelope, utc_now
from events import ErrorLogReader
from metrics import collect_global_status, collect_statement_digests, cycle_duration, heartbeat
from sinks import build_sink

log = logging.getLogger("collector")

_shutdown = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    _shutdown = True
    log.info("signal %s received; finishing the current cycle then exiting", signum)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Azure MySQL Flexible Server metrics collector")
    p.add_argument(
        "--interval", type=float, default=10.0,
        help="Seconds between samples. Benchmarks use 1-5; production 10-15.",
    )
    p.add_argument(
        "--sink", action="append", dest="sinks", default=None,
        choices=["jsonl", "adx-streaming", "adx-queued"],
        help="Repeatable. Run jsonl alongside an ADX sink so a rejected window can be replayed.",
    )
    p.add_argument("--out", default=None, help="JSONL output path (default: stdout)")
    p.add_argument(
        "--digest-interval", type=int, default=6,
        help="Collect statement digests every Nth cycle. They are heavier and change slowly.",
    )
    p.add_argument(
        "--max-cycles", type=int, default=0,
        help="Stop after N cycles (0 = run until interrupted). Used by the test harness.",
    )
    p.add_argument(
        "--cursor-file", default=".error_log_cursor.json",
        help="Where the error_log ring-buffer cursor is persisted across restarts.",
    )
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout stays clean for the jsonl sink
    )

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    log.info("configuration: %s", cfg.describe())

    sink_kinds = args.sinks or ["jsonl"]
    sinks = []
    try:
        for kind in sink_kinds:
            sinks.append(build_sink(kind, cfg, args.out))
    except Exception as exc:  # noqa: BLE001 - surface setup failures clearly and exit
        log.error("could not build sink: %s", exc)
        return 2

    try:
        conn = connect(cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("could not connect to MySQL: %s", exc)
        return 3

    try:
        cipher = assert_tls(conn)
        log.info("TLS cipher in use: %s", cipher)

        identity = server_identity(conn)
        version = identity.get("version", "unknown")
        log.info("server version %s, redo log capacity %s bytes",
                 version, identity.get("innodb_redo_log_capacity", "unknown"))
        if not version.startswith("8.4"):
            log.warning(
                "server reports version %s but this repository targets MySQL 8.4 only; "
                "metric names and variables may not match.", version,
            )

        env = Envelope(run_id=cfg.run_id, host=cfg.host, tier=cfg.tier)
        reader = ErrorLogReader(env, Path(args.cursor_file))
        reader.load_cursor(conn)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        return _run_loop(args, conn, env, reader, sinks)
    finally:
        for sink in sinks:
            sink.close()
        conn.close()
        log.info("collector stopped")


def _run_loop(args: Any, conn: Any, env: Envelope, reader: ErrorLogReader, sinks: list) -> int:
    cycle = 0
    last_cycle_seconds: float | None = None
    log.info("polling every %.1fs with sinks: %s",
             args.interval, ", ".join(s.name for s in sinks))

    while not _shutdown:
        started = time.monotonic()
        cycle += 1
        ts = utc_now()

        # The heartbeat is emitted first, before anything that can fail. That is what makes
        # "collector alive but MySQL unreachable" distinguishable from "collector dead".
        batch = [heartbeat(env, ts)]

        # The previous cycle's duration rather than this one's, because a cycle is only fully
        # measured once its sink writes finish — and by then the batch has already shipped.
        if last_cycle_seconds is not None:
            batch.append(cycle_duration(env, last_cycle_seconds, ts))

        try:
            batch.extend(collect_global_status(conn, env, ts))
            if args.digest_interval > 0 and cycle % args.digest_interval == 0:
                batch.extend(collect_statement_digests(conn, env, ts=ts))
        except Exception as exc:  # noqa: BLE001 - keep sampling; the heartbeat still ships
            log.error("metric collection failed on cycle %d: %s", cycle, exc)

        events: list = []
        try:
            events = list(reader.poll(conn))
        except Exception as exc:  # noqa: BLE001
            log.error("event collection failed on cycle %d: %s", cycle, exc)

        for sink in sinks:
            sink.write_metrics(batch)
            if events:
                sink.write_events(events)

        log.info("cycle %d: %d metrics, %d events", cycle, len(batch), len(events))

        if args.max_cycles and cycle >= args.max_cycles:
            log.info("reached --max-cycles=%d", args.max_cycles)
            break

        # Subtract the work already done so the sampling interval stays honest under load —
        # otherwise the effective interval drifts and rate calculations skew.
        elapsed = time.monotonic() - started
        last_cycle_seconds = elapsed
        sleep_for = max(0.0, args.interval - elapsed)
        if sleep_for == 0.0:
            log.warning("cycle %d took %.2fs, longer than the %.1fs interval",
                        cycle, elapsed, args.interval)
        slept = 0.0
        while slept < sleep_for and not _shutdown:
            step = min(0.25, sleep_for - slept)
            time.sleep(step)
            slept += step

    return 0


if __name__ == "__main__":
    sys.exit(main())
