-- global_status.sql
--
-- Curated allow-list of MySQL 8.4 status variables.
--
-- Read from performance_schema.global_status rather than SHOW GLOBAL STATUS because the
-- performance_schema table is filterable, so the allow-list is applied *at collection*.
-- MySQL 8.4 exposes 400+ status variables; keeping the curated set below cuts ingestion
-- volume (and ADX cost) roughly fivefold.
--
-- Names absent on a given server are silently skipped by the IN list, so this file stays
-- valid across minor versions without defensive code in the collector.
--
-- Inputs:   none (no parameters)
-- Returns:  VARIABLE_NAME (string), VARIABLE_VALUE (string; cast by the collector)
-- Consumer: collector/metrics.py -> source "global_status"

SELECT VARIABLE_NAME, VARIABLE_VALUE
FROM performance_schema.global_status
WHERE VARIABLE_NAME IN (
    -- InnoDB physical I/O — the primary Premium SSD v1 vs v2 signal
    'Innodb_data_reads',
    'Innodb_data_writes',
    'Innodb_data_read',
    'Innodb_data_written',
    'Innodb_data_fsyncs',
    'Innodb_data_pending_reads',
    'Innodb_data_pending_writes',
    'Innodb_data_pending_fsyncs',
    'Innodb_pages_read',
    'Innodb_pages_written',
    'Innodb_pages_created',

    -- Redo log (8.4: sized by innodb_redo_log_capacity)
    'Innodb_os_log_written',
    'Innodb_os_log_fsyncs',
    'Innodb_os_log_pending_fsyncs',
    'Innodb_os_log_pending_writes',
    'Innodb_log_writes',
    'Innodb_log_write_requests',
    'Innodb_log_waits',

    -- Buffer pool — separates "storage is slow" from "working set does not fit"
    'Innodb_buffer_pool_read_requests',
    'Innodb_buffer_pool_reads',
    'Innodb_buffer_pool_write_requests',
    'Innodb_buffer_pool_pages_total',
    'Innodb_buffer_pool_pages_free',
    'Innodb_buffer_pool_pages_dirty',
    'Innodb_buffer_pool_pages_data',
    'Innodb_buffer_pool_bytes_dirty',
    'Innodb_buffer_pool_wait_free',

    -- Row activity and lock contention
    'Innodb_rows_read',
    'Innodb_rows_inserted',
    'Innodb_rows_updated',
    'Innodb_rows_deleted',
    'Innodb_row_lock_waits',
    'Innodb_row_lock_time',
    'Innodb_row_lock_time_avg',
    'Innodb_row_lock_current_waits',

    -- Connections and threads
    'Threads_connected',
    'Threads_running',
    'Threads_created',
    'Threads_cached',
    'Connections',
    'Aborted_connects',
    'Aborted_clients',
    'Max_used_connections',

    -- Statement throughput
    'Queries',
    'Questions',
    'Slow_queries',
    'Com_select',
    'Com_insert',
    'Com_update',
    'Com_delete',
    'Com_commit',
    'Com_rollback',
    'Com_begin',
    'Prepared_stmt_count',

    -- Network
    'Bytes_received',
    'Bytes_sent',

    -- Handler counters — reveal scan-heavy access patterns
    'Handler_read_first',
    'Handler_read_key',
    'Handler_read_next',
    'Handler_read_rnd',
    'Handler_read_rnd_next',
    'Handler_write',
    'Handler_update',
    'Handler_delete',
    'Handler_commit',
    'Handler_rollback',

    -- Temporary objects — disk temp tables are a storage-tier sensitive cost
    'Created_tmp_tables',
    'Created_tmp_disk_tables',
    'Created_tmp_files',

    -- Query shape
    'Select_scan',
    'Select_full_join',
    'Select_range',
    'Sort_merge_passes',
    'Sort_rows',
    'Sort_scan',

    -- Table cache and locking
    'Table_locks_waited',
    'Table_locks_immediate',
    'Table_open_cache_hits',
    'Table_open_cache_misses',
    'Open_tables',
    'Opened_tables',

    -- Binary log
    'Binlog_cache_use',
    'Binlog_cache_disk_use',

    -- TLS — Azure enforces require_secure_transport=ON; these prove it is actually in use
    'Ssl_accepts',
    'Ssl_finished_accepts',

    -- Server lifetime — a drop in Uptime means a restart, which resets every counter above
    'Uptime'
);
