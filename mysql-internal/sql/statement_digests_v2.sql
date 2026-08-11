-- Digest hashes preserve correlation without exporting SQL or customer-shaped sample text.
SELECT
    SCHEMA_NAME,
    DIGEST,
    COUNT_STAR,
    SUM_TIMER_WAIT,
    SUM_LOCK_TIME,
    SUM_ROWS_EXAMINED,
    SUM_ROWS_SENT,
    SUM_ERRORS,
    SUM_WARNINGS,
    SUM_CREATED_TMP_TABLES,
    SUM_CREATED_TMP_DISK_TABLES,
    SUM_NO_INDEX_USED,
    QUANTILE_95,
    QUANTILE_99
FROM performance_schema.events_statements_summary_by_digest
WHERE DIGEST IS NOT NULL
ORDER BY SUM_TIMER_WAIT DESC
LIMIT %s
