PRAGMA foreign_keys = ON;

-- Single-use records of OIDC transactions (docs/ACCOUNTS_DESIGN.md, sections 5 and 13;
-- docs/reviews/OIDC_CONSUMPTION_SCOPE.md). Operational state: never exported to
-- data/canonical.
--
-- Storage only. Nothing reads or writes this table yet: the service that records a
-- consumption (web/services/oidc_consumption.py) has no caller until provider sign-in
-- arrives with a later PR. One table is added, no index and no row; no other table is
-- touched.
--
-- tx_hash is HMAC-SHA256 under the application secret, in lower-case hexadecimal, of
-- the purpose and the value that was used: a state, nonce, code or token is never
-- stored. Its conditions together mean "exactly 64 bytes, each of them one of 0-9a-f",
-- and each is needed:
--   * length(tx_hash) counts the characters before the first NUL, and GLOB stops reading
--     at that NUL too. Those two alone accept 64 hexadecimal characters followed by a
--     NUL and anything at all.
--   * length(CAST(tx_hash AS BLOB)) counts every byte. It alone accepts a 64-byte value
--     with a NUL inside it, and GLOB never sees what follows the NUL.
--   * 64 characters in 64 bytes leaves room for neither a NUL nor a multi-byte
--     character, so GLOB then sees every byte.
--
-- expires_at is the expiry of the object the key was derived from, in whole seconds of
-- the callers' monotonic clock. It is not a wall-clock time. The column has INTEGER
-- affinity, so SQLite converts a value that reads as a whole number ('600', 600.0)
-- before the CHECK sees it: what is stored is always an integer of at least 0.
--
-- Nothing else is stored: no user, purpose, address or wall-clock time, and nothing
-- that says which run of the service wrote a row. Rows are removed by their expiry
-- only. WITHOUT ROWID and no secondary index: the table holds the sign-ins of a few
-- minutes.
CREATE TABLE IF NOT EXISTS oidc_consumed_transactions (
    tx_hash TEXT PRIMARY KEY
        CHECK (typeof(tx_hash) = 'text'
               AND length(tx_hash) = 64
               AND length(CAST(tx_hash AS BLOB)) = 64
               AND tx_hash NOT GLOB '*[^0-9a-f]*'),
    expires_at INTEGER NOT NULL
        CHECK (typeof(expires_at) = 'integer' AND expires_at >= 0)
) WITHOUT ROWID;
