"""Collection Group implementations for the version-2 telemetry contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from catalog import CATALOG, GLOBAL_STATUS_NAMES
from connection import load_sql
from plan import GroupPlan
from telemetry import TelemetryContext, TelemetryPoint

PICOSECONDS_PER_MS = 1_000_000_000
SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")
Collector = Callable[
    [Any, TelemetryContext, GroupPlan, datetime], list[TelemetryPoint]
]


def _rows(conn: Any, sql_name: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with conn.cursor() as cursor:
        cursor.execute(load_sql(sql_name), params)
        return list(cursor.fetchall())


def _point(
    context: TelemetryContext,
    observed_at: datetime,
    measurement: str,
    fields: dict[str, Any],
    tags: dict[str, str] | None = None,
) -> TelemetryPoint:
    point = TelemetryPoint(
        observed_at=observed_at,
        context=context,
        measurement=measurement,
        fields=fields,
        tags=tags or {},
    )
    return CATALOG.validate(point)


def collect_global_status(
    conn: Any, context: TelemetryContext, _plan: GroupPlan, observed_at: datetime
) -> list[TelemetryPoint]:
    placeholders = ",".join(["%s"] * len(GLOBAL_STATUS_NAMES))
    sql = load_sql("global_status_v2.sql").format(placeholders=placeholders)
    with conn.cursor() as cursor:
        cursor.execute(sql, GLOBAL_STATUS_NAMES)
        rows = cursor.fetchall()
    fields: dict[str, float] = {}
    for name, raw_value in rows:
        try:
            fields[name] = float(raw_value)
        except (TypeError, ValueError):
            continue
    return (
        [_point(context, observed_at, "mysql.global_status", fields)]
        if fields
        else []
    )


def collect_file_io(
    conn: Any, context: TelemetryContext, plan: GroupPlan, observed_at: datetime
) -> list[TelemetryPoint]:
    points = []
    for row in _rows(conn, "file_io.sql", (plan.top_k or 100,)):
        (
            event,
            read_count,
            read_bytes,
            read_wait,
            write_count,
            write_bytes,
            write_wait,
            misc_count,
            misc_wait,
        ) = row
        modes = (
            ("read", read_count, read_bytes, read_wait),
            ("write", write_count, write_bytes, write_wait),
            ("misc", misc_count, 0, misc_wait),
        )
        for mode, operations, byte_count, wait_ps in modes:
            points.append(
                _point(
                    context,
                    observed_at,
                    "mysql.file_io",
                    {
                        "operations_total": int(operations or 0),
                        "bytes_total": int(byte_count or 0),
                        "wait_ms_total": float(wait_ps or 0) / PICOSECONDS_PER_MS,
                    },
                    {"event": str(event), "mode": mode},
                )
            )
    return points


def collect_statement_digests(
    conn: Any, context: TelemetryContext, plan: GroupPlan, observed_at: datetime
) -> list[TelemetryPoint]:
    points = []
    for row in _rows(conn, "statement_digests_v2.sql", (plan.top_k or 50,)):
        (
            schema,
            digest,
            executions,
            latency,
            lock_time,
            rows_examined,
            rows_sent,
            errors,
            warnings,
            tmp_tables,
            tmp_disk_tables,
            no_index,
            p95,
            p99,
        ) = row
        points.append(
            _point(
                context,
                observed_at,
                "mysql.statement_digest",
                {
                    "executions_total": int(executions or 0),
                    "latency_ms_total": float(latency or 0) / PICOSECONDS_PER_MS,
                    "lock_ms_total": float(lock_time or 0) / PICOSECONDS_PER_MS,
                    "rows_examined_total": int(rows_examined or 0),
                    "rows_sent_total": int(rows_sent or 0),
                    "errors_total": int(errors or 0),
                    "warnings_total": int(warnings or 0),
                    "tmp_tables_total": int(tmp_tables or 0),
                    "tmp_disk_tables_total": int(tmp_disk_tables or 0),
                    "no_index_used_total": int(no_index or 0),
                    "latency_p95_ms": float(p95 or 0) / PICOSECONDS_PER_MS,
                    "latency_p99_ms": float(p99 or 0) / PICOSECONDS_PER_MS,
                },
                {"schema": str(schema or "(none)"), "digest": str(digest)},
            )
        )
    return points


def collect_innodb_metrics(
    conn: Any, context: TelemetryContext, _plan: GroupPlan, observed_at: datetime
) -> list[TelemetryPoint]:
    points = []
    for name, subsystem, metric_type, value in _rows(conn, "innodb_metrics.sql"):
        field = (
            "counter_value"
            if metric_type in ("counter", "status_counter")
            else "gauge_value"
        )
        converted: int | float = (
            int(value or 0) if field == "counter_value" else float(value or 0)
        )
        points.append(
            _point(
                context,
                observed_at,
                "mysql.innodb_metric",
                {field: converted},
                {
                    "name": str(name),
                    "subsystem": str(subsystem),
                    "metric_type": str(metric_type),
                },
            )
        )
    return points


def collect_global_variables(
    conn: Any, context: TelemetryContext, _plan: GroupPlan, observed_at: datetime
) -> list[TelemetryPoint]:
    points = []
    for name, raw_value in _rows(conn, "global_variables.sql"):
        try:
            fields: dict[str, Any] = {"numeric_value": float(raw_value)}
        except (TypeError, ValueError):
            fields = {"text_value": str(raw_value)}
        points.append(
            _point(
                context,
                observed_at,
                "mysql.global_variable",
                fields,
                {"name": str(name)},
            )
        )
    return points


def collect_process_states(
    conn: Any, context: TelemetryContext, _plan: GroupPlan, observed_at: datetime
) -> list[TelemetryPoint]:
    return [
        _point(
            context,
            observed_at,
            "mysql.process_state",
            {"sessions": int(sessions), "oldest_seconds": int(oldest or 0)},
            {"command": str(command or "(none)"), "state": str(state or "(none)")},
        )
        for command, state, sessions, oldest in _rows(conn, "process_states.sql")
    ]


def collect_schema_size(
    conn: Any, context: TelemetryContext, plan: GroupPlan, observed_at: datetime
) -> list[TelemetryPoint]:
    return [
        _point(
            context,
            observed_at,
            "mysql.schema_size",
            {
                "rows_estimate": int(rows or 0),
                "data_bytes": int(data_bytes or 0),
                "index_bytes": int(index_bytes or 0),
                "free_bytes": int(free_bytes or 0),
            },
            {"schema": str(schema), "table": str(table), "engine": str(engine)},
        )
        for schema, table, engine, rows, data_bytes, index_bytes, free_bytes in _rows(
            conn, "schema_size.sql", (*SYSTEM_SCHEMAS, plan.top_k or 500)
        )
    ]


def collect_table_io(
    conn: Any, context: TelemetryContext, plan: GroupPlan, observed_at: datetime
) -> list[TelemetryPoint]:
    points = []
    for row in _rows(conn, "table_io.sql", (*SYSTEM_SCHEMAS, plan.top_k or 100)):
        schema, table, *values = row
        points.append(
            _point(
                context,
                observed_at,
                "mysql.table_io",
                {
                    "fetch_total": int(values[0] or 0),
                    "fetch_wait_ms_total": float(values[1] or 0)
                    / PICOSECONDS_PER_MS,
                    "insert_total": int(values[2] or 0),
                    "insert_wait_ms_total": float(values[3] or 0)
                    / PICOSECONDS_PER_MS,
                    "update_total": int(values[4] or 0),
                    "update_wait_ms_total": float(values[5] or 0)
                    / PICOSECONDS_PER_MS,
                    "delete_total": int(values[6] or 0),
                    "delete_wait_ms_total": float(values[7] or 0)
                    / PICOSECONDS_PER_MS,
                },
                {"schema": str(schema), "table": str(table)},
            )
        )
    return points


def collect_index_io(
    conn: Any, context: TelemetryContext, plan: GroupPlan, observed_at: datetime
) -> list[TelemetryPoint]:
    return [
        _point(
            context,
            observed_at,
            "mysql.index_io",
            {
                "fetch_total": int(fetches or 0),
                "fetch_wait_ms_total": float(wait_ps or 0) / PICOSECONDS_PER_MS,
            },
            {"schema": str(schema), "table": str(table), "index": str(index)},
        )
        for schema, table, index, fetches, wait_ps in _rows(
            conn, "index_io.sql", (*SYSTEM_SCHEMAS, plan.top_k or 100)
        )
    ]


GROUP_COLLECTORS: dict[str, Collector] = {
    "global-status": collect_global_status,
    "innodb-metrics": collect_innodb_metrics,
    "global-variables": collect_global_variables,
    "file-io": collect_file_io,
    "process-states": collect_process_states,
    "statement-digests": collect_statement_digests,
    "schema-size": collect_schema_size,
    "table-io": collect_table_io,
    "index-io": collect_index_io,
}
