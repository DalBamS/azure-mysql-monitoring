-- ps_statement_digest.sql
--
-- Top statement digests by total latency, from performance_schema.
--
-- These are CUMULATIVE aggregates, not counters that reset per interval. The collector
-- snapshots them and diffs against the previous snapshot, so a rate can be derived without
-- resetting server-side state (which would corrupt any other consumer).
--
-- Latency columns are picoseconds in performance_schema; the collector converts to
-- milliseconds so dashboards never carry a unit conversion.
--
-- Digest text is truncated deliberately: full statement text can embed customer data, and
-- this repo ships telemetry for gaming customers.
--
-- Inputs:   %s -> row limit (top N digests by total latency)
-- Returns:  Digest, DigestText, CountStar, SumTimerWait, SumRowsExamined, SumRowsSent,
--           SumNoIndexUsed, SumLockTime
-- Consumer: collector/metrics.py -> source "ps_digest"

SELECT
    DIGEST                                AS Digest,
    LEFT(DIGEST_TEXT, 255)                AS DigestText,
    COUNT_STAR                            AS CountStar,
    SUM_TIMER_WAIT                        AS SumTimerWait,
    SUM_ROWS_EXAMINED                     AS SumRowsExamined,
    SUM_ROWS_SENT                         AS SumRowsSent,
    SUM_NO_INDEX_USED                     AS SumNoIndexUsed,
    SUM_LOCK_TIME                         AS SumLockTime
FROM performance_schema.events_statements_summary_by_digest
WHERE DIGEST IS NOT NULL
  AND SCHEMA_NAME NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
ORDER BY SUM_TIMER_WAIT DESC
LIMIT %s;
