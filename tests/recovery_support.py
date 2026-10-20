"""SQL fixtures only; scope 8.1 has no production recovery creator."""
import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import timedelta
from pathlib import Path

from src import manage_accounts, manage_migrations
from tests.support import ROOT, build_test_database
from tests.registration_support import T0, stamp

MIGRATION = "025_password_recoveries.sql"


def add_recovery(conn, **changes):
    values = dict(binding_hash="a" * 64, state="pending", is_shadow=1,
                  email="fixture@example.bg", code_hash="b" * 64,
                  code_expires_at=stamp(T0 + timedelta(minutes=10)),
                  created_at=stamp(T0), expires_at=stamp(T0 + timedelta(minutes=30)))
    values.update(changes)
    return conn.execute(f"INSERT INTO password_recoveries ({', '.join(values)}) "
                        f"VALUES ({', '.join('?' for _ in values)})", tuple(values.values())).lastrowid


def add_send(conn, **changes):
    values = dict(email_hash="c" * 64, kind="code", attempted_at=stamp(T0),
                  issuance_id="d" * 32)
    values.update(changes)
    return conn.execute(f"INSERT INTO registration_sends ({', '.join(values)}) "
                        f"VALUES ({', '.join('?' for _ in values)})", tuple(values.values())).lastrowid


def cli(path, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = manage_accounts.main(["--db", str(path), *map(str, args)])
    return status, out.getvalue() + err.getvalue()


def historical(root, level, name="history.db"):
    migrations = root / (name + "-migrations")
    migrations.mkdir()
    for source in (ROOT / "web/migrations").glob("*.sql"):
        if int(source.name[:3]) <= level:
            shutil.copyfile(source, migrations / source.name)
    path = root / name
    sqlite3.connect(path).close()
    status = manage_migrations.apply_migrations(path, migrations, out=io.StringIO())
    assert status == 0
    return path


class RecoveryTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="recovery-8-1-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = build_test_database(self.root / "test.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.conn.close)
