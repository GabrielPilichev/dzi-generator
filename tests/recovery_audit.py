"""Opt-in RZ1 whole-suite audit, including subprocesses in a scratch interpreter.

Install with ``install(log_path)`` from that interpreter's sitecustomize. Never
imported by application code. SQL INSERTs into recovery storage are forbidden
unless the immediate caller is a recovery test/fixture, or (scope 8.2) the
recovery service ``web.services.recovery`` while a recovery test or fixture is on
the same thread's stack: the service has no production caller, so a service
insert reached from anything else (an application, CLI or other test path) is
still a violation. On close/collection, every supported nonfixture database must
have zero recovery rows and zero recovery-purpose ledger rows
(``registration_sends.recovery_id`` set). Reports only counts and source labels,
never paths, SQL values or credentials.
"""
import atexit
import inspect
import json
import os
import sqlite3
import weakref

_installed = False


_SERVICE_MODULE = "web.services.recovery"


def _fixture_module(name):
    return (name.startswith("tests.test_recovery_") or name.startswith("test_recovery_")
            or name in ("tests.recovery_support", "tests.recovery_service_support"))


def _recovery_test_on_stack(frame):
    while frame is not None:
        if _fixture_module(frame.f_globals.get("__name__", "")):
            return True
        frame = frame.f_back
    return False


def install(log_path):
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
                    frame = inspect.currentframe().f_back
                    while frame and frame.f_globals.get("__name__") == __name__:
                        frame = frame.f_back
                    caller = frame.f_globals.get("__name__", "") if frame else ""
                    allowed = _fixture_module(caller) or (
                        caller == _SERVICE_MODULE and _recovery_test_on_stack(frame))
                    if not allowed:
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
