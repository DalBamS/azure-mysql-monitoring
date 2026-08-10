-- error_log_bootstrap.sql
--
-- Establishes the initial cursor for the incremental error_log read.
--
-- On a cold start the collector must NOT ingest the entire ring buffer: those entries are
-- historical, often predate the current RUN_ID, and would arrive with timestamps far outside
-- the benchmark window. Start from the newest entry instead and move forward from there.
--
-- Returns NULL when the buffer is empty; the collector then falls back to "now".
--
-- Inputs:   none
-- Returns:  Cursor (TIMESTAMP(6) or NULL)
-- Consumer: collector/events.py

SELECT MAX(LOGGED) AS Cursor
FROM performance_schema.error_log;
