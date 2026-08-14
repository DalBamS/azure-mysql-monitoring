"""Repository-owned metric catalog.

Collection SQL says how to read MySQL. This catalog says what the observations *mean* after they
leave MySQL, so ADX functions and dashboards never have to guess whether a value is a counter,
gauge, byte count or duration.
"""

from __future__ import annotations

from telemetry import Cardinality, FieldKind, FieldSpec, MeasurementSpec, MetricCatalog


GLOBAL_STATUS_NAMES = (
    "Innodb_data_reads",
    "Innodb_data_writes",
    "Innodb_data_read",
    "Innodb_data_written",
    "Innodb_data_fsyncs",
    "Innodb_data_pending_reads",
    "Innodb_data_pending_writes",
    "Innodb_data_pending_fsyncs",
    "Innodb_pages_read",
    "Innodb_pages_written",
    "Innodb_pages_created",
    "Innodb_os_log_written",
    "Innodb_os_log_fsyncs",
    "Innodb_os_log_pending_fsyncs",
    "Innodb_os_log_pending_writes",
    "Innodb_log_writes",
    "Innodb_log_write_requests",
    "Innodb_log_waits",
    "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_reads",
    "Innodb_buffer_pool_write_requests",
    "Innodb_buffer_pool_pages_total",
    "Innodb_buffer_pool_pages_free",
    "Innodb_buffer_pool_pages_dirty",
    "Innodb_buffer_pool_pages_data",
    "Innodb_buffer_pool_bytes_dirty",
    "Innodb_buffer_pool_wait_free",
    "Innodb_rows_read",
    "Innodb_rows_inserted",
    "Innodb_rows_updated",
    "Innodb_rows_deleted",
    "Innodb_row_lock_waits",
    "Innodb_row_lock_time",
    "Innodb_row_lock_time_avg",
    "Innodb_row_lock_current_waits",
    "Threads_connected",
    "Threads_running",
    "Threads_created",
    "Threads_cached",
    "Connections",
    "Aborted_connects",
    "Aborted_clients",
    "Max_used_connections",
    "Queries",
    "Questions",
    "Slow_queries",
    "Com_select",
    "Com_insert",
    "Com_update",
    "Com_delete",
    "Com_commit",
    "Com_rollback",
    "Com_begin",
    "Prepared_stmt_count",
    "Bytes_received",
    "Bytes_sent",
    "Handler_read_first",
    "Handler_read_key",
    "Handler_read_next",
    "Handler_read_rnd",
    "Handler_read_rnd_next",
    "Handler_write",
    "Handler_update",
    "Handler_delete",
    "Handler_commit",
    "Handler_rollback",
    "Created_tmp_tables",
    "Created_tmp_disk_tables",
    "Created_tmp_files",
    "Select_scan",
    "Select_full_join",
    "Select_range",
    "Sort_merge_passes",
    "Sort_rows",
    "Sort_scan",
    "Table_locks_waited",
    "Table_locks_immediate",
    "Table_open_cache_hits",
    "Table_open_cache_misses",
    "Open_tables",
    "Opened_tables",
    "Binlog_cache_use",
    "Binlog_cache_disk_use",
    "Ssl_accepts",
    "Ssl_finished_accepts",
    "Uptime",
)

_GAUGES = {
    "Innodb_data_pending_reads",
    "Innodb_data_pending_writes",
    "Innodb_data_pending_fsyncs",
    "Innodb_os_log_pending_fsyncs",
    "Innodb_os_log_pending_writes",
    "Innodb_buffer_pool_pages_total",
    "Innodb_buffer_pool_pages_free",
    "Innodb_buffer_pool_pages_dirty",
    "Innodb_buffer_pool_pages_data",
    "Innodb_buffer_pool_bytes_dirty",
    "Innodb_row_lock_current_waits",
    "Innodb_row_lock_time_avg",
    "Threads_connected",
    "Threads_running",
    "Threads_cached",
    "Max_used_connections",
    "Prepared_stmt_count",
    "Open_tables",
    "Uptime",
}

_BYTE_FIELDS = {
    "Innodb_data_read",
    "Innodb_data_written",
    "Innodb_os_log_written",
    "Innodb_buffer_pool_bytes_dirty",
    "Bytes_received",
    "Bytes_sent",
}

_MILLISECOND_FIELDS = {"Innodb_row_lock_time", "Innodb_row_lock_time_avg"}


def _global_status_field(name: str) -> FieldSpec:
    if name in _BYTE_FIELDS:
        unit = "bytes"
    elif name in _MILLISECOND_FIELDS:
        unit = "ms"
    elif "pages" in name.lower():
        unit = "pages"
    elif name.startswith("Threads_"):
        unit = "threads"
    elif name == "Uptime":
        unit = "s"
    else:
        unit = "operations"
    return FieldSpec(
        kind=FieldKind.GAUGE if name in _GAUGES else FieldKind.COUNTER,
        unit=unit,
        value_type=float,
        required=False,
    )


CATALOG = MetricCatalog(
    [
        MeasurementSpec(
            name="collector.health",
            description="Collector liveness, MySQL reachability and completed-cycle duration.",
            fields={
                "heartbeat": FieldSpec(FieldKind.GAUGE, "state", float),
                "mysql_reachable": FieldSpec(FieldKind.GAUGE, "state", float),
                "cycle_duration_ms": FieldSpec(
                    FieldKind.GAUGE, "ms", float, required=False
                ),
            },
        ),
        MeasurementSpec(
            name="mysql.global_status",
            description="Curated SHOW GLOBAL STATUS counters and gauges.",
            fields={name: _global_status_field(name) for name in GLOBAL_STATUS_NAMES},
        ),
        MeasurementSpec(
            name="mysql.file_io",
            description="performance_schema file IO operations, bytes and cumulative wait.",
            tags=frozenset({"event", "mode"}),
            cardinality=Cardinality.BOUNDED,
            fields={
                "operations_total": FieldSpec(FieldKind.COUNTER, "operations", int),
                "bytes_total": FieldSpec(FieldKind.COUNTER, "bytes", int),
                "wait_ms_total": FieldSpec(FieldKind.COUNTER, "ms", float),
            },
        ),
        MeasurementSpec(
            name="mysql.statement_digest",
            description="Bounded statement aggregates without literal SQL values.",
            tags=frozenset({"schema", "digest"}),
            cardinality=Cardinality.HIGH,
            fields={
                "executions_total": FieldSpec(FieldKind.COUNTER, "executions", int),
                "latency_ms_total": FieldSpec(FieldKind.COUNTER, "ms", float),
                "lock_ms_total": FieldSpec(FieldKind.COUNTER, "ms", float),
                "rows_examined_total": FieldSpec(FieldKind.COUNTER, "rows", int),
                "rows_sent_total": FieldSpec(FieldKind.COUNTER, "rows", int),
                "errors_total": FieldSpec(FieldKind.COUNTER, "errors", int),
                "warnings_total": FieldSpec(FieldKind.COUNTER, "warnings", int),
                "tmp_tables_total": FieldSpec(FieldKind.COUNTER, "tables", int),
                "tmp_disk_tables_total": FieldSpec(FieldKind.COUNTER, "tables", int),
                "no_index_used_total": FieldSpec(FieldKind.COUNTER, "executions", int),
                "latency_p95_ms": FieldSpec(FieldKind.GAUGE, "ms", float),
                "latency_p99_ms": FieldSpec(FieldKind.GAUGE, "ms", float),
            },
        ),
        MeasurementSpec(
            name="mysql.innodb_metric",
            description="Enabled INFORMATION_SCHEMA.INNODB_METRICS observations.",
            tags=frozenset({"name", "subsystem", "metric_type"}),
            cardinality=Cardinality.BOUNDED,
            fields={
                "counter_value": FieldSpec(
                    FieldKind.COUNTER, "value", int, required=False
                ),
                "gauge_value": FieldSpec(
                    FieldKind.GAUGE, "value", float, required=False
                ),
            },
        ),
        MeasurementSpec(
            name="mysql.global_variable",
            description="Slow-changing server configuration facts.",
            tags=frozenset({"name"}),
            cardinality=Cardinality.BOUNDED,
            fields={
                "numeric_value": FieldSpec(
                    FieldKind.GAUGE, "value", float, required=False
                ),
                "text_value": FieldSpec(
                    FieldKind.STATE, "text", str, series=False, required=False
                ),
            },
        ),
        MeasurementSpec(
            name="mysql.process_state",
            description="Aggregated connection commands and wait states.",
            tags=frozenset({"command", "state"}),
            cardinality=Cardinality.BOUNDED,
            fields={
                "sessions": FieldSpec(FieldKind.GAUGE, "sessions", int),
                "oldest_seconds": FieldSpec(FieldKind.GAUGE, "s", int),
            },
        ),
        MeasurementSpec(
            name="mysql.schema_size",
            description="Table capacity and row estimates from INFORMATION_SCHEMA.TABLES.",
            tags=frozenset({"schema", "table", "engine"}),
            cardinality=Cardinality.HIGH,
            fields={
                "rows_estimate": FieldSpec(FieldKind.GAUGE, "rows", int),
                "data_bytes": FieldSpec(FieldKind.GAUGE, "bytes", int),
                "index_bytes": FieldSpec(FieldKind.GAUGE, "bytes", int),
                "free_bytes": FieldSpec(FieldKind.GAUGE, "bytes", int),
            },
        ),
        MeasurementSpec(
            name="mysql.table_io",
            description="Table IO operation counts and cumulative waits.",
            tags=frozenset({"schema", "table"}),
            cardinality=Cardinality.HIGH,
            fields={
                "fetch_total": FieldSpec(FieldKind.COUNTER, "operations", int),
                "fetch_wait_ms_total": FieldSpec(FieldKind.COUNTER, "ms", float),
                "insert_total": FieldSpec(FieldKind.COUNTER, "operations", int),
                "insert_wait_ms_total": FieldSpec(FieldKind.COUNTER, "ms", float),
                "update_total": FieldSpec(FieldKind.COUNTER, "operations", int),
                "update_wait_ms_total": FieldSpec(FieldKind.COUNTER, "ms", float),
                "delete_total": FieldSpec(FieldKind.COUNTER, "operations", int),
                "delete_wait_ms_total": FieldSpec(FieldKind.COUNTER, "ms", float),
            },
        ),
        MeasurementSpec(
            name="mysql.index_io",
            description="Index fetch counts and cumulative waits.",
            tags=frozenset({"schema", "table", "index"}),
            cardinality=Cardinality.HIGH,
            fields={
                "fetch_total": FieldSpec(FieldKind.COUNTER, "operations", int),
                "fetch_wait_ms_total": FieldSpec(FieldKind.COUNTER, "ms", float),
            },
        ),
    ]
)
