"""Single-use consumption records for OIDC transactions.

Specification: docs/reviews/OIDC_CONSUMPTION_SCOPE.md, and docs/ACCOUNTS_DESIGN.md
sections 5 and 13. Storage only: no route, sign-in flow or other module calls it yet.

An OAuth transaction lives in the signed session cookie, and the server cannot take a
cookie back: removing the transaction from a response does not stop an earlier copy
of the cookie from presenting it again. ``consume`` records in
``oidc_consumed_transactions`` (migration 020) that a key was used, so that a second
presentation of the same key is refused. Only a keyed hash is stored: never a state,
nonce, verifier, code or token, and nothing about a user.

Independent of Flask, ``web.app`` and the blueprints: standard library only. It reads
no clock and writes no log. ``consume`` takes an open ``sqlite3`` connection that is
already inside a transaction, and never commits or rolls back: the caller owns the
transaction and the clock.

**A key is accepted when ``consume`` has returned AND the caller's COMMIT has
succeeded, and not before.** Until that commit the record is one more pending write
of the caller's transaction.

Caller obligations
------------------

``consume`` cannot check any of these. "A key is accepted at most once" holds only
when every caller meets all of them. They are numbered as in the specification.

1. **Lock, then clock.** Execute ``BEGIN IMMEDIATE`` first, read the clock once after
   that statement has returned, and pass that reading as ``now``. Never pass a reading
   taken before the write lock was held: a caller that waited for the lock would hold
   a stale ``now``, and a stale ``now`` can pass the expiry check for a key whose row
   another caller's cleanup has removed in the meantime. ``RETENTION_ALLOWANCE`` is a
   margin for a caller that gets this wrong, not a licence to.
2. **One clock.** ``now`` and every ``expires_at`` are whole seconds of one monotonic
   clock that every process writing to the database shares: ``int(time.monotonic())``
   of one host within one boot. Never the wall clock. Two hosts, or two Linux time
   namespaces, do not share that clock.
3. **A fixed, authenticated expiry.** ``expires_at`` is the expiry stored in the
   object the key was derived from, read after that object's integrity has been
   verified, and passed unchanged. It is never computed at the time of consumption
   and never taken from a request, so one key has one ``expires_at`` in every call
   that ever names it. An expired object is refused (``Expired``); the caller does not
   retry it with a later expiry.
4. **Only the run that issued it.** Before calling ``consume``, refuse every object
   that the current run of the service did not issue. A restart, a reboot and a
   restore each begin a new run.
5. **Acceptance is the return of** ``consume`` **and a successful COMMIT.** Everything
   else means "do not proceed": both refusals, ``ValueError``, ``RuntimeError``, every
   ``sqlite3.Error``, and a commit that fails or never happens. No error is ever read
   as "not used yet".
6. **Finish the transaction** after every outcome, by committing or rolling back.
   After ``AlreadyConsumed`` the transaction is still usable: only the refused INSERT
   is undone, while this call's cleanup and the caller's own earlier writes are still
   pending. A consumption that is rolled back is not an acceptance, and its key is
   unused again.
7. **Keys come only from** ``consumption_key``, with one of ``PURPOSES``.
8. **Nothing else deletes rows:** no other code, no scheduled job and no operator
   procedure. A row removed by anything but the cleanup of ``consume`` makes its key
   usable again until that key's expiry.

What the storage does not do
----------------------------

It records keys and refuses a repeated one. Alone it does not

* bind a transaction to a browser: whether the request that presents a value is the
  one the value was issued to is the caller's check;
* invalidate anything at a restart. Rows of an ended run are ordinary rows. After a
  reboot the monotonic clock starts again near zero, and the storage alone would
  accept an *unused* object of the earlier boot for as long as its old ``expires_at``
  exceeds the new clock. Obligation 4 is what refuses it;
* make a restore safe. A restored snapshot lacks every row written after it was
  taken. Only a restore that begins a new run (the supported procedure stops and
  starts the service) together with obligation 4 makes a lost row harmless.

Those are obligations of the code that will call this module, and they arrive with it.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3

PURPOSE_CALLBACK = "callback"
PURPOSE_LINK_CONFIRM = "link-confirm"
# The set is fixed. Adding a purpose is a change to this module with its own review.
PURPOSES = frozenset({PURPOSE_CALLBACK, PURPOSE_LINK_CONFIRM})

# Seconds a row is kept after its expiry before the cleanup of ``consume`` may delete
# it. It covers a caller whose ``now`` is up to this stale (obligation 1).
RETENTION_ALLOWANCE = 60

_MAX_VALUE_LENGTH = 128
# Exclusive bound of ``now`` and ``expires_at``: far inside SQLite's 64-bit integers,
# so no sum or difference of two accepted times can leave them.
_TIME_LIMIT = 2**62

_KEY_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
# The alphabet of ``secrets.token_urlsafe``. Neither it nor a purpose contains the NUL
# that separates the parts of the message, so two different (purpose, value) pairs
# never produce the same message.
_URLSAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_MESSAGE_PREFIX = b"oidc-consume\0"
# SQLite's extended result code SQLITE_CONSTRAINT_PRIMARYKEY (19 | 6 << 8), written out
# because the sqlite3 module names it only from Python 3.11 on.
_SQLITE_CONSTRAINT_PRIMARYKEY = 1555


class ConsumptionRefused(Exception):
    """The key may not be used. Nothing was recorded for it by this call."""


class AlreadyConsumed(ConsumptionRefused):
    """A record of this key is already stored.

    The stored row is unchanged and nothing was added. Only the refused INSERT is
    undone: the caller's transaction is still open and usable, with this call's
    cleanup and the caller's earlier writes pending in it. The caller finishes it.
    """


class Expired(ConsumptionRefused):
    """``now`` has reached ``expires_at``. No statement was executed."""


def consumption_key(secret_key, purpose, value) -> str:
    """The 64-character lower-case hex key that ``value`` is recorded under.

    ``HMAC-SHA256(secret_key, b"oidc-consume\\0" + purpose + b"\\0" + value)``.
    ``secret_key`` is non-empty ``bytes``, or a non-empty ``str`` that is encoded as
    UTF-8. ``purpose`` is a member of ``PURPOSES``. ``value`` is a ``str`` of 1 to 128
    characters of ``A-Z``, ``a-z``, ``0-9``, ``-`` and ``_``. Anything else is a
    ``ValueError``: nothing is trimmed, folded or repaired, and no message repeats what
    was passed. The two purposes give different keys for one value.
    """
    if isinstance(secret_key, str):
        try:
            secret_key = secret_key.encode("utf-8")
        except UnicodeEncodeError:
            # Refused below, outside this handler: the encoding error carries the whole
            # secret (its ``object``) and must not travel with the ValueError.
            secret_key = None
    if not isinstance(secret_key, bytes) or not secret_key:
        raise ValueError("the secret key must be non-empty bytes, or a non-empty str that encodes as UTF-8")
    if not isinstance(purpose, str) or purpose not in PURPOSES:
        raise ValueError("unknown consumption purpose")
    if (not isinstance(value, str) or not 1 <= len(value) <= _MAX_VALUE_LENGTH
            or not all(ch in _URLSAFE for ch in value)):
        raise ValueError(f"the value must be 1 to {_MAX_VALUE_LENGTH} URL-safe characters")
    message = _MESSAGE_PREFIX + purpose.encode("ascii") + b"\0" + value.encode("ascii")
    return hmac.new(secret_key, message, hashlib.sha256).hexdigest()


def _is_key(value) -> bool:
    return isinstance(value, str) and len(value) == _KEY_LENGTH and all(ch in _HEX_DIGITS for ch in value)


def _is_time(value) -> bool:
    # ``type(...) is int``: a bool is an int, and True must not pass as second 1.
    return type(value) is int and 0 <= value < _TIME_LIMIT


def consume(conn: sqlite3.Connection, key: str, *, now: int, expires_at: int) -> None:
    """Record that ``key`` was used, inside the caller's transaction; return ``None``.

    In this order:

    1. ``ValueError`` unless ``key`` is 64 lower-case hexadecimal characters and
       ``now`` and ``expires_at`` are each an ``int`` (not a ``bool``) from 0 to below
       ``2**62``.
    2. ``RuntimeError`` unless ``conn`` is inside a transaction the caller has begun.
    3. :class:`Expired` when ``now >= expires_at``. Nothing is read, written or
       deleted, so an expired key is refused before the cleanup could ever matter.
    4. Cleanup: every row with ``expires_at < now - RETENTION_ALLOWANCE`` is deleted.
       Expiry is the only condition: which run or connection wrote a row plays no part.
    5. A plain INSERT of ``(key, expires_at)``. :class:`AlreadyConsumed` when, and
       only when, the primary key refuses it as a duplicate.

    Every other ``sqlite3.Error`` propagates unchanged, another integrity failure
    included: a missing table, a read-only connection, a lock that was not obtained.
    Nothing is replaced, upserted, committed, rolled back or logged.

    The duplicate is recognised by SQLite's result code, which ``sqlite3`` exposes
    from Python 3.11 on. On an older interpreter a duplicate propagates as the
    ``sqlite3.IntegrityError`` it is: still refused, but not as ``AlreadyConsumed``.

    Returning is not acceptance: the caller's COMMIT must succeed as well. See the
    module documentation for that and for everything else the caller owes.
    """
    if not _is_key(key):
        raise ValueError("the consumption key must be 64 lower-case hexadecimal characters")
    if not _is_time(now) or not _is_time(expires_at):
        raise ValueError("now and expires_at must be whole seconds: an int from 0 to below 2**62")
    if not conn.in_transaction:
        raise RuntimeError("consume() needs a transaction that the caller has already begun")
    if now >= expires_at:
        raise Expired("the object this key was derived from has expired")
    conn.execute("DELETE FROM oidc_consumed_transactions WHERE expires_at < ?", (now - RETENTION_ALLOWANCE,))
    try:
        conn.execute("INSERT INTO oidc_consumed_transactions (tx_hash, expires_at) VALUES (?, ?)", (key, expires_at))
    except sqlite3.IntegrityError as exc:
        # The extended result code decides, never the error class or its text: a CHECK,
        # a trigger or another unique index failing is a storage fault, not a replay.
        # Before Python 3.11 the code is not exposed, and the duplicate then propagates
        # as the IntegrityError it is: refused all the same, never accepted.
        if getattr(exc, "sqlite_errorcode", None) == _SQLITE_CONSTRAINT_PRIMARYKEY:
            raise AlreadyConsumed("this key has already been used") from exc
        raise
