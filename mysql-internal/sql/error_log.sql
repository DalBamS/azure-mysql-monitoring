-- error_log.sql
--
-- Incremental read of performance_schema.error_log.
--
-- This is the ONLY route to MySQL error-log content on Azure Database for MySQL Flexible
-- Server: diagnostic settings expose just "MySQL Audit Logs" and "MySQL Slow Logs", and
-- there is no filesystem to read.
--
-- The table is a RING BUFFER — entries are evicted as new ones arrive, so a slow poll loses
-- data permanently. Read incrementally using the last seen LOGGED value as a cursor and
-- persist that cursor so a collector restart neither skips nor duplicates entries.
--
-- LOGGED is TIMESTAMP(6); the strict > comparison relies on microsecond precision, which is
-- why the cursor must be stored at full precision rather than truncated to seconds.
--
-- Inputs:   %s -> cursor, the last LOGGED value already ingested (UTC)
--           %s -> row limit, bounding a single poll after a long stall
-- Returns:  LOGGED, PRIO, ERROR_CODE, SUBSYSTEM, DATA
-- Consumer: collector/events.py -> source "error_log"

SELECT
    LOGGED,
    PRIO,
    ERROR_CODE,
    SUBSYSTEM,
    DATA
FROM performance_schema.error_log
WHERE LOGGED > %s
ORDER BY LOGGED ASC
LIMIT %s;
