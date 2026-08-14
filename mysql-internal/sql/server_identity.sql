-- server_identity.sql
--
-- One-shot snapshot of server identity and the MySQL 8.4 configuration this repo asserts.
--
-- The collector runs this once at startup and logs the result. It doubles as a live check of
-- the assumptions the rest of the repo is built on: version 8.4, the redo-log variable rename,
-- the default auth plugin, and enforced TLS.
--
-- Inputs:   none
-- Returns:  VARIABLE_NAME, VARIABLE_VALUE
-- Consumer: collector/connection.py, testing/verify.py

SELECT VARIABLE_NAME, VARIABLE_VALUE
FROM performance_schema.global_variables
WHERE VARIABLE_NAME IN (
    'version',
    'version_comment',
    'hostname',
    'require_secure_transport',      -- Azure sets this ON; clients must use TLS
    'default_authentication_plugin', -- 8.4: caching_sha2_password
    'innodb_redo_log_capacity',      -- 8.4: replaces innodb_log_file_size
    'innodb_buffer_pool_size',
    'innodb_io_capacity',
    'innodb_io_capacity_max',
    'innodb_flush_log_at_trx_commit',
    'innodb_flush_method',
    'max_connections',
    'long_query_time',
    'slow_query_log',
    'performance_schema',
    'time_zone'
);
