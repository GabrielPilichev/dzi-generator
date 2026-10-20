"""RR8–14/16–22. Offline, real SQLite inputs; no developer database."""
import builtins
import hashlib
import itertools
import os
import shutil
import sqlite3
from contextlib import closing
from unittest.mock import patch

from src import manage_accounts as commands, restore_credential_diff as diff, backup_sqlite
from tests.recovery_support import RecoveryTestCase, cli, historical
from web.services import accounts, totp


class RestoreCredentialsTest(RecoveryTestCase):
    def setUp(self):
        super().setUp()
        self.uid = accounts.create_user(self.conn, display_name="Fixture User", email="owner@example.bg",
                                       password="fixture password 1234")
        self.conn.execute("UPDATE users SET password_changed_at='2026-01-01 00:00:00'")
        self.conn.commit()
        self.replaced = self.root / "replaced.db"
        shutil.copyfile(self.path, self.replaced)
        # Merely stat'ed; its contents deliberately are not a database.
        self.live = self.root / "live.db"
        self.live.write_bytes(b"must never open")

    def run_diff(self, snapshot=None, replaced=None, *extra):
        return cli(self.live, "restore-credential-diff", "--snapshot", snapshot or self.path,
                   "--replaced", replaced or self.replaced, *extra)

    def test_separate_dispatch_immutable_inputs_no_live_open_and_no_secrets(self):
        before = {p: p.read_bytes() for p in (self.path, self.replaced, self.live)}
        real_connect, real_open, real_os_open = sqlite3.connect, builtins.open, os.open
        seen = []
        def connect(path, *args, **kwargs):
            self.assertNotIn(str(self.live), str(path))
            self.assertIn("mode=ro&immutable=1", str(path))
            seen.append(str(path))
            return real_connect(path, *args, **kwargs)
        def file_open(path, *args, **kwargs):
            self.assertNotEqual(str(path), str(self.live))
            return real_open(path, *args, **kwargs)
        def os_open(path, *args, **kwargs):
            self.assertNotEqual(str(path), str(self.live))
            return real_os_open(path, *args, **kwargs)
        with patch.object(commands, "open_database", side_effect=AssertionError("live open")), \
                patch.object(sqlite3, "connect", side_effect=connect), \
                patch.object(builtins, "open", side_effect=file_open), patch.object(os, "open", side_effect=os_open):
            code, output = self.run_diff()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(seen), 2)
        self.assertNotIn("restore-credential-diff", commands.COMMANDS)
        self.assertNotIn("restore-credential-diff", commands.READ_ONLY_COMMANDS)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
            for suffix in commands.SQLITE_SIDECAR_SUFFIXES:
                self.assertFalse(os.path.lexists(str(path)+suffix))
        for value in self.conn.execute("SELECT password_hash, session_key FROM users").fetchone():
            self.assertNotIn(value, output)

    def test_real_password_state_totp_changes_and_feasible_repair_commands(self):
        with closing(sqlite3.connect(self.replaced)) as conn:
            accounts.set_password(conn, self.uid, "new fixture password")
            conn.execute("UPDATE users SET password_changed_at='2026-01-02 00:00:00'")
            accounts.set_active(conn, self.uid, False)
            key = totp.Fernet.generate_key().decode()
            totp.enroll(conn, self.uid, key, "owner@example.bg")
            conn.commit()
            secrets = conn.execute("SELECT password_hash, session_key FROM users").fetchone()
            ciphertext = conn.execute("SELECT secret_ciphertext FROM totp_credentials").fetchone()[0]
        code, output = self.run_diff()
        self.assertEqual(code, 1)
        for label in ("password reverted", "category P", "state differs", "second factor differs",
                      "set-password --email owner@example.bg", "disable --id", "totp-reset --email"):
            self.assertIn(label, output)
        for secret in (*secrets, ciphertext):
            self.assertNotIn(secret, output)
        with patch.object(commands, "read_password", return_value="replacement fixture password"):
            self.assertEqual(cli(self.path, "set-password", "--email", "owner@example.bg")[0], 0)
        for command, args in (("disable", ("--id", self.uid)), ("set-role", ("--id", self.uid, "--role", "student")),
                              ("totp-reset", ("--email", "owner@example.bg")), ("sign-out", ("--id", self.uid))):
            self.assertEqual(cli(self.path, command, *args)[0], 0)

    def test_unavailable_replaced_is_uncertain_snapshot_unavailable_has_no_classes(self):
        code, output = self.run_diff(replaced=self.root / "absent.db")
        self.assertEqual(code, 1)
        self.assertIn("uncertain (no evidence)", output)
        self.assertIn("category P", output)
        code, output = self.run_diff(snapshot=self.root / "absent.db")
        self.assertEqual(code, 1)
        self.assertIn("every account of the live database", output)
        self.assertNotIn("category", output)

    def test_empty_short_bad_header_and_all_sidecars_refused_before_open(self):
        for content in (b"", b"garbage", b"SQLite format 3\0"+b"\0\0\1\1"+b"\0"*30):
            bad = self.root / "bad.db"
            bad.write_bytes(content)
            with patch.object(diff.sqlite3, "connect", side_effect=AssertionError("SQLite opened bad header")):
                self.assertEqual(self.run_diff(snapshot=bad)[0], 1)
        # Why the complete header is our own check: helper + SQLite accept empty.
        bad.write_bytes(b"")
        commands.require_inspectable_snapshot(bad)
        with closing(sqlite3.connect(bad.as_uri()+"?mode=ro&immutable=1", uri=True)) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        for suffix, content in itertools.product(commands.SQLITE_SIDECAR_SUFFIXES, (b"", b"committed")):
            for side in (self.path, self.replaced):
                sidecar = side.with_name(side.name+suffix)
                sidecar.write_bytes(content)
                try:
                    with patch.object(diff.sqlite3, "connect", side_effect=AssertionError("SQLite opened sidecar input")):
                        with self.assertRaises(diff.Unavailable):
                            diff.read_input(side, (-1,-1), commands.require_inspectable_snapshot, commands.OperatorError)
                    code, output = self.run_diff()
                    self.assertEqual(code, 1)
                    self.assertIn("evidence unavailable", output)
                    self.assertNotIn("unchanged", output)
                finally:
                    sidecar.unlink()

    def test_links_live_identity_and_expected_hash_rejected(self):
        for name, source, symbolic in (("symbolic.db", self.path, True), ("hard.db", self.live, False)):
            link = self.root / name
            link.symlink_to(source) if symbolic else os.link(source, link)
            self.assertEqual(self.run_diff(snapshot=link)[0], 1)
        with patch.object(diff.sqlite3, "connect", side_effect=AssertionError("hash precheck skipped")):
            self.assertEqual(self.run_diff(None, None, "--snapshot-sha256", "0"*64)[0], 1)
        self.assertEqual(self.run_diff(snapshot=self.live)[0], 1)

    def test_changing_inputs_no_stable_comparison(self):
        # Equal brackets do not detect a change made and undone. Operator-only,
        # quiescent preparation is required and this test claims no such proof.
        original = diff._read_rows
        for mutation in ("append", "rewrite", "rename", "mtime", "journal", "wal"):
            candidate = self.root / (mutation+".db")
            shutil.copyfile(self.path, candidate)
            def read(conn):
                data = original(conn)
                if mutation == "append":
                    with candidate.open("ab") as f: f.write(b"x")
                elif mutation == "rewrite":
                    with candidate.open("r+b") as f:
                        f.seek(99); f.write(b"x")
                elif mutation == "rename":
                    replacement = self.root / "renamed.db"
                    shutil.copyfile(candidate, replacement)
                    replacement.replace(candidate)
                elif mutation == "mtime":
                    info = candidate.stat()
                    os.utime(candidate, ns=(info.st_atime_ns, info.st_mtime_ns+1000000))
                else:
                    candidate.with_name(candidate.name+"-"+mutation).write_bytes(b"x")
                return data
            with self.subTest(mutation=mutation), patch.object(diff, "_read_rows", side_effect=read):
                code, output = self.run_diff(snapshot=candidate)
            self.assertEqual(code, 1)
            self.assertNotIn("category", output)

    def test_wal_only_commit_is_refused_then_online_backup_includes_it(self):
        with closing(sqlite3.connect(self.replaced)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            old = conn.execute("SELECT password_hash FROM users").fetchone()[0]
            accounts.set_password(conn, self.uid, "wal committed password")
            conn.execute("UPDATE users SET password_changed_at='2026-01-02 00:00:00'")
            conn.commit()
            with closing(sqlite3.connect(self.replaced.as_uri()+"?mode=ro&immutable=1", uri=True)) as stale:
                self.assertEqual(stale.execute("SELECT password_hash FROM users").fetchone()[0], old)
            self.assertNotEqual(conn.execute("SELECT password_hash FROM users").fetchone()[0], old)
            with patch.object(diff.sqlite3, "connect", side_effect=AssertionError("WAL input opened")):
                with self.assertRaises(diff.Unavailable):
                    diff.read_input(self.replaced, (-1,-1), commands.require_inspectable_snapshot, commands.OperatorError)
            prepared = self.root / "prepared.db"
            backup_sqlite.backup_database(self.replaced, prepared)
            code, output = self.run_diff(replaced=prepared)
            self.assertEqual(code, 1)
            self.assertIn("password reverted", output)
        # Checkpointed WAL header with no sidecars is still refused.
        self.assertFalse(self.replaced.with_name("replaced.db-wal").exists())
        self.assertIn("WAL-mode", self.run_diff()[1])

    def test_historical_levels_and_absent_storage(self):
        for level in (11, 12, 15, 16, 21, 23):
            path = historical(self.root, level, f"level-{level}.db")
            with closing(sqlite3.connect(path)) as conn:
                if level >= 12:
                    row = self.conn.execute("SELECT id,email,display_name,role,password_hash,is_active,session_key,password_changed_at FROM users").fetchone()
                    conn.execute("INSERT INTO users(id,email,display_name,role,password_hash,is_active,session_key,password_changed_at) VALUES(?,?,?,?,?,?,?,?)", row)
                    conn.commit()
            code, output = self.run_diff(snapshot=path)
            self.assertEqual(code, 0, output)
            self.assertIn("created after" if level == 11 else "unchanged (as far as compared)", output)
            if level >= 12:
                self.assertEqual(self.run_diff(replaced=path)[0], 0)

    def test_missing_required_columns_and_recorded_storage_fail_closed_extra_accepted(self):
        mutations = ["DROP TABLE users", "DROP TABLE totp_credentials", "DROP TABLE totp_recovery_codes",
                     "ALTER TABLE users RENAME COLUMN session_key TO missing",
                     "ALTER TABLE users RENAME COLUMN password_changed_at TO missing",
                     "ALTER TABLE users RENAME COLUMN password_hash TO missing",
                     "ALTER TABLE totp_credentials RENAME COLUMN secret_ciphertext TO missing"]
        for index, mutation in enumerate(mutations):
            bad = self.root / f"unsupported-{index}.db"
            shutil.copyfile(self.path, bad)
            with closing(sqlite3.connect(bad)) as conn:
                conn.execute(mutation); conn.commit()
            with self.subTest(mutation=mutation):
                code, output = self.run_diff(snapshot=bad)
                self.assertEqual(code, 1)
                self.assertNotIn("category", output)
                self.assertIn("uncertain (no evidence)", self.run_diff(replaced=bad)[1])
        with closing(sqlite3.connect(self.replaced)) as conn:
            conn.execute("ALTER TABLE users ADD COLUMN future_column TEXT"); conn.commit()
        self.assertEqual(self.run_diff()[0], 0)


    def test_no_db_required_path_and_env_fallback_and_readonly_inputs(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        with patch.dict(os.environ, {}, clear=True), patch.object(diff, "read_input", side_effect=AssertionError("input read")), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(commands.main(["restore-credential-diff", "--snapshot", str(self.path),
                                           "--replaced", str(self.replaced)]), 1)
        with patch.dict(os.environ, {"DZI_DB": str(self.live)}), redirect_stdout(io.StringIO()):
            self.assertEqual(commands.main(["restore-credential-diff", "--snapshot", str(self.path),
                                           "--replaced", str(self.replaced)]), 0)
        self.path.chmod(0o400)
        self.replaced.chmod(0o400)
        self.root.chmod(0o500)
        try:
            self.assertEqual(self.run_diff()[0], 0)
        finally:
            self.root.chmod(0o700)
            self.path.chmod(0o600)
            self.replaced.chmod(0o600)

    def test_live_wal_file_and_sidecars_only_stated_never_opened(self):
        self.live.unlink()
        with closing(sqlite3.connect(self.live)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE marker(x)")
            conn.execute("INSERT INTO marker VALUES(1)")
            conn.commit()
            paths = [self.live, self.live.with_name("live.db-wal"), self.live.with_name("live.db-shm")]
            before = [p.read_bytes() for p in paths]
            self.assertEqual(self.run_diff()[0], 0)
            self.assertEqual([p.read_bytes() for p in paths], before)

    def test_pre016_admin_rotation_and_historical_totp_difference(self):
        old = historical(self.root, 15, "admin-before.db")
        with closing(sqlite3.connect(old)) as conn:
            accounts.create_user(conn, display_name="Old Admin", email="old@example.bg",
                                 password="admin fixture password", role="admin")
            conn.commit()
        newer = self.root / "admin-after.db"
        shutil.copyfile(old, newer)
        from src import manage_migrations
        import io
        from tests.support import ROOT
        self.assertEqual(manage_migrations.apply_migrations(newer, ROOT/"web/migrations", out=io.StringIO()), 0)
        code, output = self.run_diff(snapshot=old, replaced=newer)
        self.assertEqual(code, 1)
        self.assertIn("sessions revived", output)  # migration 016's conservative false positive
        with closing(sqlite3.connect(newer)) as conn:
            totp.enroll(conn, 1, totp.Fernet.generate_key().decode(), "old@example.bg")
            conn.commit()
        self.assertIn("second factor differs", self.run_diff(snapshot=old, replaced=newer)[1])

    def test_absence_without_bookkeeping_or_totp_without_users_is_unsupported(self):
        for kind in ("no_bookkeeping", "totp_without_users"):
            bad = historical(self.root, 11, kind+".db")
            with closing(sqlite3.connect(bad)) as conn:
                if kind == "no_bookkeeping":
                    conn.execute("DROP TABLE schema_migrations")
                else:
                    conn.execute("CREATE TABLE totp_credentials(user_id,secret_ciphertext,confirmed_at)")
                conn.commit()
            self.assertEqual(self.run_diff(snapshot=bad)[0], 1)
            self.assertIn("uncertain (no evidence)", self.run_diff(replaced=bad)[1])

    def test_passwordless_identity_and_totp_recovery_code_consumption(self):
        uid = accounts.create_user(self.conn, display_name="Google Fixture")
        self.conn.execute("INSERT INTO totp_credentials(user_id,secret_ciphertext) VALUES(?,?)", (self.uid,"secret-ciphertext-marker"))
        self.conn.execute("INSERT INTO totp_recovery_codes(user_id,code_hash) VALUES(?,?)", (self.uid,"secret-recovery-code-marker"))
        self.conn.commit()
        shutil.copyfile(self.path, self.replaced)
        with closing(sqlite3.connect(self.replaced)) as conn:
            conn.execute("UPDATE totp_recovery_codes SET used_at='2026-01-02 00:00:00'")
            conn.execute("UPDATE users SET password_changed_at='2026-01-02 00:00:00' WHERE id=?", (uid,))
            conn.commit()
        code, output = self.run_diff()
        self.assertEqual(code, 1)
        self.assertIn("second factor differs", output)
        self.assertIn("category N", output)
        self.assertNotIn("set-password", output)
        self.assertNotIn("secret-ciphertext-marker", output)
        self.assertNotIn("secret-recovery-code-marker", output)

    def test_signin_hash_upgrade_without_timestamp_change_is_uncertain(self):
        from werkzeug.security import generate_password_hash
        with closing(sqlite3.connect(self.replaced)) as conn:
            old = generate_password_hash("legacy fixture password", method="pbkdf2:sha256:1000")
            conn.execute("UPDATE users SET password_hash=?", (old,))
            conn.commit()
        shutil.copyfile(self.replaced, self.path)
        with closing(sqlite3.connect(self.replaced)) as conn:
            self.assertIsNotNone(accounts.authenticate(conn, "owner@example.bg", "legacy fixture password"))
            conn.commit()
        self.assertIn("uncertain (password evidence)", self.run_diff()[1])


class ClassificationTest(RecoveryTestCase):
    def test_exhaustive_precedence_categories_and_findings(self):
        hashes = (None, "one", "two")
        stamps = (None, "2026-01-01 00:00:00", "2026-01-02 00:00:00", "malformed")
        cases = 0
        for h1, h2, t1, t2, keys, state, factor, identity in itertools.product(
                hashes, hashes, stamps, stamps, (False, True), (False, True), (False, True),
                ("both", "snapshot", "replaced", "other_email", "unavailable")):
            a = dict(id=1, email="a@example.bg", password_hash=h1, password_changed_at=t1,
                     session_key="old", role="teacher", is_active=1)
            b = dict(a, password_hash=h2, password_changed_at=t2, session_key="new" if keys else "old",
                     role="student" if state else "teacher")
            if identity == "other_email": b["email"] = "other@example.bg"
            aa = None if identity == "replaced" else a
            bb = None if identity == "snapshot" else b
            order, category = diff.classify(aa, bb, unavailable=identity=="unavailable")
            if identity == "unavailable": expected=1
            elif identity in ("snapshot", "other_email"): expected=2
            elif identity == "replaced": expected=3
            elif h1 == h2 and t1 != t2: expected=4
            elif h1 != h2:
                expected = 6 if h1 is not None and h2 is not None and t1 == stamps[1] and t2 == stamps[2] else 5
            else: expected=7 if keys else 8
            self.assertEqual(order, expected)
            expected_category = None if expected in (3,8) else "K" if expected==7 else "N" if h1 is None else "P"
            self.assertEqual(category, expected_category)
            snap = diff.Evidence({1:aa} if aa else {}, {})
            repl = None if identity=="unavailable" else diff.Evidence({1:bb} if bb else {},
                       {1:(frozenset({("ciphertext-secret", None)}), frozenset())} if factor else {})
            lines, status = diff.report(snap,repl)
            output = "\n".join(lines)
            if aa and aa["password_hash"] is None: self.assertNotIn("set-password", output)
            self.assertNotIn("ciphertext-secret", output)
            self.assertEqual("state differs" in output, aa is not None and bb is not None and repl is not None and state)
            self.assertEqual("second factor differs" in output, aa is not None and bb is not None and repl is not None and factor)
            self.assertEqual(status, int(category is not None or (aa is not None and bb is not None and repl is not None and (state or factor))))
            cases += 1
        self.assertEqual(cases, 5760)
