-- The field list and semantics live in collector/catalog.py. The collector expands only this
-- repository-owned placeholder and binds every value as a query parameter.
SELECT VARIABLE_NAME, VARIABLE_VALUE
FROM performance_schema.global_status
WHERE VARIABLE_NAME IN ({placeholders})
