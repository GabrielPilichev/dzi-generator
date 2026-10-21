"""Opt-in RZ1 whole-suite audit, including subprocesses in a scratch interpreter.

Install with ``install(log_path)`` from that interpreter's sitecustomize. Never
imported by application code. SQL INSERTs into recovery storage are forbidden
unless the first caller outside this module is a recovery test/fixture, or the
recovery service ``web.services.recovery`` was entered from an allowed caller: the
audit walks past its own frames and the contiguous service frames, and the next
frame's module must be

* a recovery test/fixture module (scope 8.2), or
* (scope 8.3) **the one production caller, the blueprint** ``web.blueprints.recovery``,
  and then only while a recovery test/fixture module is also somewhere up the same
  thread's stack, i.e. a recovery test drives the route. A route reached from any other
  test (a sweep, a smoke test) is not a recovery test and its insert is a violation.

Any other first caller (an application module, the CLI, another test path or a
production-named intermediary between a recovery test or the blueprint and the service)
is a violation: "any caller allowed" is not the policy, and the audit is not disabled.
On close/collection, every supported nonfixture database must have zero recovery rows and
zero recovery-purpose ledger rows (``registration_sends.recovery_id`` set). Reports only
counts and source labels, never paths, SQL values or credentials.

**Prepared-statement cache (scope 8.3).** SQLite calls the authorizer when it *prepares* a
statement, not when it executes one, and Python's ``sqlite3`` keeps a per-connection cache
of prepared statements. With the cache, an INSERT prepared and allowed for a fixture caller
is reused for a later caller with the same SQL text without another check. ``install``
therefore opens every connection with ``cached_statements=0`` by default
(``fresh_prepare=True``), so each execution prepares afresh and is authorized for its own
caller. ``fresh_prepare=False`` restores the cache and its documented blind spot (a test
pins both). Not covered either way: connections not opened through ``sqlite3.connect`` in an
interpreter that loads this hook, and writes that do not go through an authorizer-visible
INSERT of ``password_recoveries`` (the static scans cover those).
"""
import atexit
import inspect
import json
import os
import sqlite3
import weakref

_installed = False


_SERVICE_MODULE = "web.services.recovery"


_BLUEPRINT_MODULE = "web.blueprints.recovery"


def _fixture_module(name):
    return (name.startswith("tests.test_recovery_") or name.startswith("test_recovery_")
            or name in ("tests.recovery_support", "tests.recovery_service_support",
                        "tests.recovery_route_support"))


def _insert_caller(frame):
    """Module name of the first frame outside the audit and the service's own frames."""
    while frame is not None and frame.f_globals.get("__name__") in (__name__, _SERVICE_MODULE):
        frame = frame.f_back
    return frame.f_globals.get("__name__", "") if frame else ""


def _recovery_test_above(frame):
    """Whether a recovery test/fixture module is anywhere up this thread's stack."""
    while frame is not None:
        if _fixture_module(frame.f_globals.get("__name__", "")):
            return True
        frame = frame.f_back
    return False


def _insert_allowed(frame):
    """Whether an INSERT into recovery storage prepared from ``frame`` may proceed."""
    caller = _insert_caller(frame)
    if _fixture_module(caller):
        return True
    # The blueprint is the one production caller; a route is allowed only while a recovery test drives it.
    return caller == _BLUEPRINT_MODULE and _recovery_test_above(frame)


def install(log_path, *, fresh_prepare=True):
    global _installed
    if _installed:
        return
    _installed = True
    original = sqlite3.connect
    live = weakref.WeakSet()
    counts = dict(connections=0, checked_empty=0, fixture_storage=0,
                  absent_storage=0, unreadable_or_closed=0, violations=0)
    classes = {}

    def violation():
        counts["violations"] += 1

    class Audited:
        _recovery_audited = False
        _recovery_fixture = False
        _recovery_trace = None

        def set_trace_callback(self, callback):
            self._recovery_trace = callback
            return super().set_trace_callback(callback)

        def set_authorizer(self, callback):
            def authorize(action, table, column, db, trigger):
                if action == sqlite3.SQLITE_INSERT and table == "password_recoveries":
                    if not _insert_allowed(inspect.currentframe().f_back):
                        violation()
                        return sqlite3.SQLITE_DENY
                    self._recovery_fixture = True
                return callback(action, table, column, db, trigger) if callback else sqlite3.SQLITE_OK
            return super().set_authorizer(authorize)

        def _audit_recovery(self):
            if self._recovery_audited:
                return
            self._recovery_audited = True
            try:
                # Audit reads are test instrumentation, not application SQL.
                # Keep them out of callers' query budgets/transaction traces;
                # every application statement still reaches the original trace.
                sqlite3.Connection.set_trace_callback(self, None)
                found = sqlite3.Connection.execute(self,
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='password_recoveries'").fetchone()
                if not found:
                    counts["absent_storage"] += 1
                elif self._recovery_fixture:
                    counts["fixture_storage"] += 1
                else:
                    rows = sqlite3.Connection.execute(self, "SELECT COUNT(*) FROM password_recoveries").fetchone()[0]
                    sends = {row[1] for row in sqlite3.Connection.execute(
                        self, "PRAGMA table_info(registration_sends)")}
                    if "recovery_id" in sends:
                        rows += sqlite3.Connection.execute(
                            self, "SELECT COUNT(*) FROM registration_sends WHERE recovery_id IS NOT NULL"
                        ).fetchone()[0]
                    if rows:
                        violation()
                        raise AssertionError("RZ1: nonfixture recovery storage is not empty")
                    counts["checked_empty"] += 1
            except sqlite3.Error:
                # Malformed/offline-rejection fixtures and connections explicitly
                # closed via a base-class call cannot be inspected at this point.
                counts["unreadable_or_closed"] += 1
            finally:
                try:
                    sqlite3.Connection.set_trace_callback(self, self._recovery_trace)
                except sqlite3.Error:
                    pass  # already closed by a test's base-class call

        def close(self):
            try:
                self._audit_recovery()
            finally:
                super().close()

        def __del__(self):
            try:
                self._audit_recovery()
            except Exception:
                pass  # violation already recorded; report at interpreter exit

    def connect(*args, **kwargs):
        base = kwargs.get("factory", sqlite3.Connection)
        if base not in classes:
            classes[base] = type("RecoveryAudited" + base.__name__, (Audited, base), {})
        kwargs["factory"] = classes[base]
        if fresh_prepare and len(args) < 7:
            # Prepare afresh at every execution, so the authorizer sees each caller (see above).
            kwargs["cached_statements"] = 0
        conn = original(*args, **kwargs)
        counts["connections"] += 1
        conn._recovery_fixture = any(_fixture_module(frame.frame.f_globals.get("__name__", ""))
                                     for frame in inspect.stack())
        conn.set_authorizer(None)
        live.add(conn)
        return conn

    sqlite3.connect = connect

    def finish():
        for conn in list(live):
            try:
                conn._audit_recovery()
            except Exception:
                pass
        with open(log_path, "a", encoding="utf-8") as output:
            output.write(json.dumps(dict(pid=os.getpid(), **counts)) + "\n")
    atexit.register(finish)
