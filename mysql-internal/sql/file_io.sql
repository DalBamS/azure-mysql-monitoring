-- Bounded file event summaries. The table contains event classes, not customer file names.
SELECT
    EVENT_NAME,
    COUNT_READ,
    SUM_NUMBER_OF_BYTES_READ,
    SUM_TIMER_READ,
    COUNT_WRITE,
    SUM_NUMBER_OF_BYTES_WRITE,
    SUM_TIMER_WRITE,
    COUNT_MISC,
    SUM_TIMER_MISC
FROM performance_schema.file_summary_by_event_name
WHERE COUNT_STAR > 0
ORDER BY SUM_TIMER_WAIT DESC
LIMIT %s
