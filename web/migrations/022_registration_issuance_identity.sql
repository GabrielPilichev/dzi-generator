PRAGMA foreign_keys = ON;

-- Issuance identity of the registration send ledger
-- (docs/reviews/EMAIL_REGISTRATION_DESIGN.md, sections 4.3 and 6.3; registration PR 3).
-- Runtime state: registration_sends is never exported to data/canonical.
--
-- Why: registration_sends.id is the send generation, but a number is not an identity.
-- When a transaction that issued a code is rolled back, SQLite gives the same ids to the
-- next issuance (the sqlite_sequence change is rolled back with it), and the six digits
-- of two codes can coincide. A result object kept from the rolled-back transaction must
-- match nothing that was committed later, for sending and for settlement alike.
--
-- issuance_id is 32 lower-case hexadecimal characters: 128 bits from the operating
-- system's random source, made anew for every issuance and resend by the registration
-- service. It is derived from nothing: not from the code, the address, the row ids or a
-- key. It is not a credential (it is never shown to a person, sent in a message or
-- accepted from a request) and says nothing about the code or the message, neither of
-- which is stored anywhere in clear.
--
-- The CHECK means "NULL, or exactly 32 bytes, each of them one of 0-9a-f"; the three
-- conditions are needed together for the reasons given in migration 020.
--
-- Existing rows: the column is added NULL and no row is written. NULL means "issued
-- before identities existed". Such a row keeps everything it had and still counts in
-- every cap and in the circuit exactly as before, but the service never sends it and
-- never settles it: nothing can present an identity that equals NULL. No identity is
-- invented for an old row, here or later. (No such row exists in any deployment: the
-- registration service has had no caller.)
--
-- The partial unique index makes a repeated identity impossible rather than unlikely,
-- and leaves any number of old NULL rows alone. No table is rebuilt; ALTER TABLE ADD
-- COLUMN accepts a CHECK because the default, NULL, satisfies it for every old row.
ALTER TABLE registration_sends ADD COLUMN issuance_id TEXT
    CHECK (issuance_id IS NULL
           OR (typeof(issuance_id) = 'text'
               AND length(issuance_id) = 32
               AND length(CAST(issuance_id AS BLOB)) = 32
               AND issuance_id NOT GLOB '*[^0-9a-f]*'));

CREATE UNIQUE INDEX idx_registration_sends_issuance
    ON registration_sends(issuance_id) WHERE issuance_id IS NOT NULL;
