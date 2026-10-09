"""Assignment lifecycle: what the source itself must not contain.

Static checks over ``web/`` and ``src/`` (L37a, L53, L139, L140 of
docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, section 15.1). The behavioural halves of
these rules are in the route and operator tests; neither is accepted alone.

* every statement that assigns a lifecycle column is in the lifecycle module or
  the operator command, and none can clear or overwrite a terminal marker;
* the grace duration comes from one named constant;
* nothing of the later phases exists: no deletion of detail, no cleanup column,
  no time-source query.
"""

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import ROOT, build_test_database
from web.services import lifecycle

LIFECYCLE_MODULE = "web/services/lifecycle.py"
OPERATOR_COMMAND = "src/manage_lifecycle.py"
WRITERS = {LIFECYCLE_MODULE, OPERATOR_COMMAND}
DETAIL_TABLES = ("quiz_assignments", "quiz_attempts", "quiz_answers", "quiz_text_answers",
                 "practical_submissions", "practical_submission_files")
LATER_PHASE_NAMES = ("details_cleaned_at", "closure_upper_bound_at", "database_identity", "restore_reclean")


def python_sources():
    for base in ("web", "src"):
        for path in sorted((ROOT / base).rglob("*.py")):
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _text(node):
    """The text of a string-building expression, or ``None``. Names that hold SQL
    fragments are resolved where the rule needs them; other names become ``{}``."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else _name_text(node.left)) + \
               (right if right is not None else _name_text(node.right))
    return None


def _name_text(node):
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
    return lifecycle.OPEN_PREDICATE_SQL if name == "OPEN_PREDICATE_SQL" else "{}"


def sql_strings(source: str):
    """Every outermost string expression of a module that reads like a write statement."""
    found = []

    def visit(node):
        text = _text(node)
        if text is not None:
            normalized = " ".join(text.split())
            if re.search(r"\b(UPDATE|INSERT|DELETE|REPLACE)\b", normalized, re.IGNORECASE):
                found.append(normalized)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return found


def assigned_columns(statement: str) -> dict:
    """``{table: {column: value expression}}`` of an UPDATE's SET clause or an INSERT's column list."""
    assigned = {}
    update = re.search(r"\bUPDATE\s+(\w+)\s+SET\s+(.*?)(?:\s+WHERE\b|$)", statement, re.IGNORECASE)
    if update:
        columns = {}
        for part in re.split(r",(?![^()]*\))", update.group(2)):
            match = re.match(r"\s*(\w+)\s*=\s*(.+?)\s*$", part)
            if match:
                columns[match.group(1)] = match.group(2)
        assigned[update.group(1)] = columns
    insert = re.search(r"\bINSERT(?:\s+OR\s+\w+)?\s+INTO\s+(\w+)\s*\(([^)]*)\)", statement, re.IGNORECASE)
    if insert:
        assigned.setdefault(insert.group(1), {}).update(
            {name.strip(): "?" for name in insert.group(2).split(",") if name.strip()})
    return assigned


class LifecycleWritersTest(unittest.TestCase):
    """L37 (a)."""

    def statements(self):
        for name, source in python_sources():
            for statement in sql_strings(source):
                yield name, statement

    def test_only_the_lifecycle_module_and_the_operator_command_assign_lifecycle_columns(self):
        offenders, writers = [], set()
        for name, statement in self.statements():
            assigned = assigned_columns(statement)
            touched = set(assigned.get("quiz_assignments", {})) & set(lifecycle.LIFECYCLE_COLUMNS)
            if touched or "lifecycle_clock" in assigned or re.search(r"\bDELETE\s+FROM\s+lifecycle_clock\b", statement):
                writers.add(name)
                if name not in WRITERS:
                    offenders.append((name, statement))
        self.assertEqual(offenders, [])
        self.assertEqual(writers, WRITERS)  # and the check does find the statements it is about

    def test_no_statement_can_clear_or_overwrite_a_terminal_marker(self):
        checked = 0
        for name, statement in self.statements():
            columns = assigned_columns(statement).get("quiz_assignments", {})
            where = statement.split(" WHERE ", 1)[1] if " WHERE " in statement else ""
            for marker in lifecycle.TERMINAL_MARKER_COLUMNS:
                if marker not in columns:
                    continue
                checked += 1
                self.assertNotRegex(columns[marker], r"(?i)^null$", f"{name}: {statement}")
                # Write-once: the statement restates that the marker is still NULL.
                self.assertRegex(where, rf"\b{marker} IS NULL\b", f"{name}: {statement}")
        # The close and the two observation stamps; reconciliation's three take-overs from the
        # replaced database. Nothing writes a marker from a log record or from what an operator states.
        self.assertEqual(checked, 6)
        for name, source in python_sources():
            for marker in lifecycle.TERMINAL_MARKER_COLUMNS:
                self.assertNotRegex(source, rf"(?i)\b{marker}\s*=\s*NULL\b", name)

    def test_the_deadline_changes_only_while_open_or_on_an_unmarked_restored_row(self):
        open_or_unmarked = re.compile(
            r"closed_at IS NULL AND expiry_observed_at IS NULL AND submissions_closed_at IS NULL")
        seen = []
        for name, statement in self.statements():
            if "deadline_at" in assigned_columns(statement).get("quiz_assignments", {}):
                seen.append(name)
                self.assertRegex(statement, open_or_unmarked, f"{name}: {statement}")
        # A new row's deadline, the edit, and reconciliation from evidence A (the replaced database).
        self.assertEqual(sorted(seen), [OPERATOR_COMMAND] + [LIFECYCLE_MODULE] * 2)

    def test_l139_the_hold_and_the_retention_floor_have_exactly_their_guarded_writers(self):
        holds, floors = [], []
        for name, statement in self.statements():
            columns = assigned_columns(statement).get("quiz_assignments", {})
            if "lifecycle_hold" in columns:
                holds.append((name, statement))
            if "retention_base_at" in columns:
                floors.append((name, statement))
        # Two statements write the hold: one adds to it, one resolves one reason. Each
        # replaces the value it just read, so nothing writes the column as a whole.
        self.assertEqual([name for name, _ in holds], [OPERATOR_COMMAND, OPERATOR_COMMAND])
        for _name, statement in holds:
            self.assertIn("AND lifecycle_hold IS ?", statement)
        self.assertEqual([name for name, _ in floors], [OPERATOR_COMMAND])
        self.assertIn("AND retention_base_at IS ?", floors[0][1])
        # No request handler names either column in a form field.
        for name, source in python_sources():
            if name.startswith("web/blueprints/"):
                self.assertNotRegex(source, r"""form\.get\(\s*["'](lifecycle_hold|retention_base_at|closed_at|"""
                                            r"""closed_by_user_id|expiry_observed_at|submissions_closed_at)["']""", name)

    def test_the_open_predicate_is_written_once(self):
        definitions = [name for name, source in python_sources()
                       if re.search(r"^OPEN_PREDICATE_SQL\s*=", source, re.MULTILINE)]
        self.assertEqual(definitions, [LIFECYCLE_MODULE])
        predicate = lifecycle.OPEN_PREDICATE_SQL
        for clause in ("lifecycle_hold IS NULL", "closed_at IS NULL", "expiry_observed_at IS NULL",
                       "submissions_closed_at IS NULL", "(deadline_at IS NULL OR deadline_at > :now)"):
            self.assertIn(clause, predicate)
        users = [name for name, source in python_sources() if "OPEN_PREDICATE_SQL" in source]
        self.assertEqual(users, ["web/services/lifecycle.py", "web/services/quiz.py"])


class RestoreEvidenceTest(unittest.TestCase):
    """Second Codex review of PR #326, blockers 1 and 2: a restore hold is released from the
    replaced database and from nothing else. These are the structural halves; the behavioural
    ones are in the operator tests."""

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / OPERATOR_COMMAND).read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def function(self, name):
        return next(node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef) and node.name == name)

    def test_a_restore_reason_is_resolved_in_one_place_from_the_replaced_database(self):
        calls = []
        for function in (node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "resolve_hold_reason":
                    calls.append((function.name, ast.unparse(node.args[2])))
        # Each call names its reason as a constant: nothing resolves "whatever reasons it finds".
        self.assertEqual(sorted(calls), [("cmd_hold_resolve", "lifecycle.HOLD_CLOCK_REVIEW"),
                                         ("cmd_reconcile", "lifecycle.HOLD_RESTORE_PENDING")])
        # And the hold column has no other way down: one statement removes from it.
        removing = [node.name for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef)
                    and "token != reason" in ast.unparse(node)]
        self.assertEqual(removing, ["resolve_hold_reason"])
        # restore_unreconciled is therefore resolved by nothing in this build: it stays.
        # cmd_reconcile reads its evidence from the database it opens, row by row.
        reconcile = ast.unparse(self.function("cmd_reconcile"))
        self.assertIn("_open_pre_restore(conn, args.pre_restore_db)", reconcile)
        self.assertIn("evidence[assignment_id]", reconcile)

    def test_evidence_qualifies_as_a_whole_before_anything_is_released(self):
        # Claude review, M1 and M2. In cmd_reconcile, in this order: the clock is still suspect
        # (so recovery comes afterwards), the evidence is the later state of this database, and
        # only then is a row touched. The replaced clock is handed on before the commit.
        reconcile = ast.unparse(self.function("cmd_reconcile"))
        order = [reconcile.index(step) for step in (
            "_open_evidence(conn, args.snapshot_db, 'snapshot database', '--snapshot-db')",
            "clock.state != lifecycle.CLOCK_STATE_SUSPECT",
            "require_later_state(conn, pre, snapshot)",
            "UPDATE quiz_assignments SET",
            "resolve_hold_reason(conn, assignment_id, lifecycle.HOLD_RESTORE_PENDING)",
            "_carry_clock(conn, pre_clock, lifecycle.wall_reading())",
            "conn.commit()")]
        self.assertEqual(order, sorted(order))
        qualify = ast.unparse(self.function("require_later_state"))
        for check in ("schema_problems(pre)", "lineage_problem(conn, pre, 'the pre-restore database')",
                      "evidence_mark < reference", "evidence_mark == reference"):
            self.assertIn(check, qualify)
        # Claude review, N3: one way to show life after the snapshot, the lifecycle clock, reached
        # only past both refusals. A difference in some other table is not one.
        self.assertEqual(qualify.count("return "), 1)
        self.assertNotIn("return None", qualify)
        self.assertLess(qualify.index("evidence_mark == reference"), qualify.index("return "))
        for gone in ("content_differs", "_table_rows", "TOOLING_"):
            self.assertNotIn(gone, self.source)
        # Identity is more than the id: what never changes, and what is written once.
        from src import manage_lifecycle
        self.assertEqual(manage_lifecycle.ASSIGNMENT_FIXED + manage_lifecycle.ASSIGNMENT_SET_ONCE,
                         ("section_id", "question_count", "created_at", "question_plan_json",
                          "owner_user_id", "public_token"))
        self.assertIn("submitted_at", manage_lifecycle.ATTEMPT_SET_ONCE)
        # The snapshot is not optional.
        parser = ast.unparse(self.function("build_parser"))
        self.assertRegex(parser, r"reconcile\.add_argument\('--snapshot-db', required=True")

    def test_one_name_for_the_restore_lock_is_shared_by_everything_that_honours_it(self):
        script = (ROOT / "deploy" / "learnpilot-restore.sh").read_text(encoding="utf-8")
        self.assertIn(f"LOCK=$STATE/data/{lifecycle.RESTORE_LOCK_NAME}", script)
        for unit in ("learnpilot.service", "learnpilot-backup.service"):
            self.assertIn(f"ConditionPathExists=!/var/lib/learnpilot/data/{lifecycle.RESTORE_LOCK_NAME}",
                          (ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8"))
        for user in ("web/production.py", "src/backup_application.py"):
            source = (ROOT / user).read_text(encoding="utf-8")
            self.assertIn("restore_lock_present(", source)
            self.assertNotIn("restore-in-progress", source)      # the name itself is in one place
        # Nothing reads anything out of the lock: it forbids, and that is all it does.
        source = (ROOT / LIFECYCLE_MODULE).read_text(encoding="utf-8")
        self.assertIn("return os.path.lexists(restore_lock_path(db_path))", source)
        self.assertEqual(source.count("RESTORE_LOCK_NAME"), 2)

    def test_the_operator_command_reads_no_log_and_no_record(self):
        for gone in ("record_events", "read_record", "--record-file", "record_file", "--affected-ids",
                     "--closed-at", "--not-closed", "--deadline-at", "sha256", "hashlib", "journalctl",
                     "lifecycle event="):
            self.assertNotIn(gone, self.source, gone)
        imported = {alias.name for node in ast.walk(self.tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertFalse(imported & {"hashlib", "re", "logging"})
        # Nothing opens a file an operator names, except the audit log (append) and the databases.
        opened = [ast.unparse(node) for node in ast.walk(self.tree)
                  if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open"]
        self.assertEqual(opened, ["open(self.path, 'a', encoding='utf-8')"])
        self.assertNotRegex(self.source, r"\.read_(text|bytes)\(")

    def test_the_two_log_based_commands_are_refused_before_anything_is_opened(self):
        from src import manage_lifecycle
        self.assertEqual(set(manage_lifecycle.UNSUPPORTED_COMMANDS), {"reconcile-partial", "reconcile-record"})
        main = ast.unparse(self.function("main"))
        refusal, opening = main.index("args.command in UNSUPPORTED_COMMANDS"), main.index("open_database(")
        self.assertLess(refusal, opening)
        self.assertLess(refusal, main.index("AuditLog("))
        self.assertIn("return EXIT_UNSUPPORTED", main[refusal:opening])
        # Their subparsers take no option at all: there is nothing they could be told.
        parser = ast.unparse(self.function("build_parser"))
        self.assertRegex(parser, r"for \(?name, what\)? in UNSUPPORTED_COMMANDS\.items\(\):\n\s+commands\.add_parser\(name, ")
        for name in manage_lifecycle.UNSUPPORTED_COMMANDS:
            self.assertNotIn(f"'{name}'", parser)

    def test_the_application_does_not_treat_its_log_lines_as_a_record(self):
        for name in (LIFECYCLE_MODULE, "web/blueprints/teacher.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("lifecycle event=", source)            # still logged, for diagnosis
            self.assertNotIn("is a record of an event that happened", source)
            self.assertRegex(source, r"for diagnosis only")


class GraceConstantTest(unittest.TestCase):
    """L53."""

    def test_the_fifteen_minutes_come_from_one_named_constant(self):
        self.assertEqual(lifecycle.GRACE_SECONDS, 15 * 60)
        literal = re.compile(r"\b900\b|15\s*\*\s*60|minutes\s*=\s*15\b|15\s*мин|15\s*minutes|quarter of an hour")
        hits = []
        # Every module that decides or shows a lifecycle time. (The login throttle's own,
        # unrelated 15-minute window lives in web/services/login_throttle.py.)
        lifecycle_code = ("web/services/lifecycle.py", "web/services/quiz.py", "web/services/practical.py",
                          "web/blueprints/", "web/app.py", "web/production.py", "src/manage_lifecycle.py")
        for name, source in python_sources():
            if not name.startswith(lifecycle_code):
                continue
            hits += [(name, line.strip()) for line in source.splitlines() if literal.search(line)]
        for path in sorted((ROOT / "web" / "templates").glob("*.html")):
            hits += [(path.name, line.strip()) for line in path.read_text(encoding="utf-8").splitlines()
                     if literal.search(line)]
        self.assertEqual(hits, [(LIFECYCLE_MODULE, "GRACE_SECONDS = 15 * 60")])
        # Everything else derives it: the state function, the audit and the notices.
        for name in (LIFECYCLE_MODULE, OPERATOR_COMMAND):
            self.assertIn("GRACE_SECONDS", (ROOT / name).read_text(encoding="utf-8"))
        self.assertIn("lifecycle.grace_minutes", (ROOT / "web/templates/teacher_assignment.html").read_text("utf-8"))


class PhaseIsolationTest(unittest.TestCase):
    """L140: PR #326 contains no cleanup, no history and no time-source query."""

    def test_no_detail_row_is_ever_deleted(self):
        pattern = re.compile(r"\bDELETE\s+FROM\s+(" + "|".join(DETAIL_TABLES) + r")\b", re.IGNORECASE)
        for name, source in python_sources():
            if name.startswith("web/") or name == OPERATOR_COMMAND:
                self.assertIsNone(pattern.search(source), name)
        for name in (LIFECYCLE_MODULE, OPERATOR_COMMAND):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotRegex(source, r"\b(unlink|rmtree|remove)\(")
            self.assertNotRegex(source, r"(?i)\bDROP\s+TABLE\b")

    def test_nothing_of_the_later_phases_is_referenced(self):
        files = [name for name, _ in python_sources() if name.startswith("web/")] + [OPERATOR_COMMAND]
        for name in files:
            source = (ROOT / name).read_text(encoding="utf-8")
            for later in LATER_PHASE_NAMES:
                if later == "restore_reclean" and name in WRITERS:
                    # Named once each, only to say that this phase does not have it.
                    self.assertNotIn(later, lifecycle.HOLD_REASONS)
                    continue
                self.assertNotIn(later, source, f"{name} mentions {later}")
        migration = (ROOT / "web/migrations/018_assignment_lifecycle.sql").read_text(encoding="utf-8")
        for later in LATER_PHASE_NAMES:
            self.assertNotIn(later, migration)
        self.assertFalse(list((ROOT / "deploy" / "systemd").glob("*cleanup*")))
        self.assertFalse((ROOT / "src" / "cleanup_assignment_details.py").exists())

    def test_no_lifecycle_code_queries_a_time_source_or_the_network(self):
        banned = {"socket", "ntplib", "urllib", "http", "requests", "ssl", "subprocess"}
        for name in (LIFECYCLE_MODULE, OPERATOR_COMMAND):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    imported.add((node.module or "").split(".")[0])
            self.assertFalse(imported & banned, f"{name}: {sorted(imported & banned)}")

    def test_the_lifecycle_module_is_standard_library_only_and_below_the_web_layer(self):
        tree = ast.parse((ROOT / LIFECYCLE_MODULE).read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        self.assertFalse({m for m in modules if m.split(".")[0] in ("flask", "web", "src", "werkzeug")}, modules)
        operator = (ROOT / OPERATOR_COMMAND).read_text(encoding="utf-8")
        self.assertNotRegex(operator, r"(?m)^\s*(from|import)\s+(flask|web\.app|web\.blueprints)")

    def test_the_server_timezone_is_never_used(self):
        source = (ROOT / LIFECYCLE_MODULE).read_text(encoding="utf-8")
        for forbidden in ("datetime.now()", "date.today()", ".astimezone()", "time.localtime", "tzlocal",
                          "fromtimestamp("):
            self.assertNotIn(forbidden, source)
        self.assertIn('_NON_ZONE_NAMES = frozenset({"localtime"', source)


class DeploymentGuideTest(unittest.TestCase):
    """The documented operator procedure. Its commands are run by tests/test_lifecycle_restore_script.py;
    here: what the guide may and may not tell an operator to type."""

    @classmethod
    def setUpClass(cls):
        cls.guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
        cls.restore = cls.guide.split("## 17. Restore", 1)[1].split("## 18. Logs", 1)[0]
        cls.lifecycle = cls.guide.split("## 26. Assignment lifecycle", 1)[1].split("## 2 GB VPS sizing", 1)[0]

    @staticmethod
    def shell_blocks(text):
        return re.findall(r"```bash\n(.*?)```", text, re.DOTALL)

    def test_a_restore_is_documented_as_one_script_and_never_as_steps_to_type(self):
        # Second Codex review, blocker 4: the documented commands, typed one after the other, went
        # on to `systemctl start` after restore-hold, reconcile or verify had failed.
        blocks = self.shell_blocks(self.restore)
        self.assertEqual(len(blocks), 3)
        for block in blocks:
            commands = [" ".join(line.split()) for line in block.replace("\\\n", " ").splitlines() if line.strip()]
            self.assertEqual(len(commands), 1, block)                       # one command per block,
            self.assertTrue(commands[0].startswith(                         # and it is the script
                "bash /opt/learnpilot/app/deploy/learnpilot-restore.sh --"), block)
        typed = "\n".join(blocks)
        for by_hand in ("systemctl", "manage_lifecycle", "manage_migrations", "sqlite3", "tar ", "mv ", "install ",
                        "restore-hold", "reconcile", "verify", "complete-swap", "&&", ";", "|"):
            self.assertNotIn(by_hand, typed, by_hand)
        # Nowhere in the section is there a command that starts the service, or one that removes the lock.
        self.assertNotRegex(self.restore, r"systemctl\s+(re)?start")
        self.assertNotRegex(self.restore, r"\brm\b|\brmdir\b")
        said = " ".join(self.restore.split())
        for sentence in ("Do not carry out its steps by hand",
                         "the only command in it that starts the service is its last one",
                         "The script never starts the service after a failure",
                         "Do not pipe it into another command",
                         "There is also no way to finish a run that stopped half-way",
                         "nothing it left on disk proves that its checks were made",
                         "Do not move anything by hand and do not remove the lock",
                         "Do not put the snapshot in place yourself first",
                         "never uses them and never deletes them"):
            self.assertIn(sentence, said)
        self.assertNotIn("--complete-swap", self.guide)
        self.assertTrue((ROOT / "deploy" / "learnpilot-restore.sh").is_file())

    def test_the_described_order_is_the_order_of_the_script(self):
        # First Codex review, finding 7, kept: the restored database is migrated before restore-hold.
        # Claude review, M2: the clock is confirmed after reconciliation. H1: the database is replaced last.
        steps = self.restore.split("### What the script does", 1)[1].split("### How it ends", 1)[0]
        order = [steps.index(marker) for marker in (
            "**Pre-flight, with the service still running and nothing modified.**", "**Lock, pause backups, stop.**",
            "**Make a copy**", "**Migrate the copy**", "**Hold every assignment**", "**Reconcile**",
            "**Confirm the clock.**", "**Check and count:**", "**Replace the live state.**",
            "**Check what is now in place:**", "**Unlock and start:**")]
        self.assertEqual(order, sorted(order))
        for kept in ("PRAGMA quick_check", "`--apply`", "replaced-<stamp>/", "018", "restore-status --expect-held all",
                     f"/var/lib/learnpilot/data/{lifecycle.RESTORE_LOCK_NAME}", "--snapshot-db",
                     "The database comes last and is replaced by a single rename",
                     "it cannot be given on the command line",
                     "If anything wrote to any of them in the meantime, nothing is replaced"):
            self.assertIn(kept, " ".join(steps.split()))
        # The script does these in the same order (its own tests prove the behaviour).
        script = (ROOT / "deploy" / "learnpilot-restore.sh").read_text(encoding="utf-8")
        script_order = [script.index(marker) for marker in (
            "-m src.restore_preflight --snapshot", 'sudo mkdir -- "$LOCK"',
            'gate "stop the service" sudo systemctl stop "$SERVICE"', '"$SNAPSHOT/questions.db" "$STAGED_DB"',
            '"${MIGRATE[@]}" --apply', "restore-hold --pre-restore-db", "reconcile --pre-restore-db",
            '"${RECOVER[@]}" --verified-utc', '"${LIFECYCLE[@]}" verify', 'gate "put the restored $tree in place"',
            'sudo mv -f -- "$STAGED_DB" "$LIVE_DB"', '"${IN_PLACE[@]}" verify',
            'gate "release the restore lock"', 'if ! sudo systemctl start "$SERVICE"; then')]
        self.assertEqual(script_order, sorted(script_order))

    def test_the_guide_states_the_restore_contract(self):
        lifecycle_section = " ".join(self.lifecycle.split())
        restore_section = " ".join(self.restore.split())
        contract = {
            "ordinary logs prove nothing": "Those lines are for diagnosis. They do not establish a test's state, "
                                           "and no command reads them",
            "a missing line is not a missing event": "A missing closure line looks exactly like a test that was "
                                                     "never closed",
            "absence from the log": "a test that the log does not mention is not thereby unchanged",
            "a digest authenticates nothing": "a SHA-256 of a file says only that the file was not changed after "
                                              "the digest was taken",
            "same-second order": "It does not give the order of two changes made in the same second",
            "holds are fail-closed": "That is the design working, not a fault to be worked around",
            "some stay held": "This release has no way to release such a test",
            "I5 is not implemented": "is decision I5 of the design and is **not** implemented",
            "success is exit 0 only": "Only 0 means that a command did everything it is for",
            "no manual clearing": "**Never clear a hold by hand to bring a test back.**",
            "no override": "There is no emergency override, and none is to be improvised",
            "restart is gated": "a restore goes through the script and is never finished by `verify` followed by "
                                "a start",
            "an interrupted restore is run again": "an interrupted restore is never \"completed\": it is run again",
            "the only release": "the only command that releases a test after a restore",
            # Claude review, M1 and M2.
            "ids are not identity": "The same number is not enough",
            # Claude review, N3: only the lifecycle clock shows what came later, and the limit is stated.
            "later by the lifecycle clock": "its lifecycle clock stands later than the snapshot's",
            "an equal clock shows nothing": "one that stands exactly where the snapshot's stood shows nothing "
                                            "that came later. Both are refused",
            "other tables are not lifecycle history": "A difference in any other table is not lifecycle history",
            "a copy of the snapshot": "is refused like the plain copy it is",
            "a quiet live database is refused too": "cannot be told from a copy of the snapshot and is refused "
                                                    "like one",
            "provenance is the procedure's": "They cannot establish where it came from, and nothing a file "
                                             "contains could",
            "the clock comes after reconciliation": "`clock-recover`: as above, and after `reconcile`, never "
                                                    "before it",
            "the start-up refusal": "a restore of the database is in progress or was left unfinished",
        }
        for label, sentence in contract.items():
            self.assertIn(sentence, lifecycle_section, label)
        for sentence in ("**Every assignment in the snapshot then stays held**", "`--accept-held` releases nothing",
                         "`RESTORE COMPLETE` says that no assignment is held **because of the restore**, and "
                         "nothing more", "it is not a promise that every test accepts answers",
                         "While that directory exists the application refuses to start and no backup is taken",
                         "That holds after a reboot too",
                         # Claude review, N1, N2, N4 and N5.
                         "What the script then takes back is decided by what is on the disk, not by how far it "
                         "believes it got",
                         "the database that was live now exists only in `replaced-<stamp>/`",
                         "a backup that is running is `activating`, which `is-active` does not count as active",
                         "The second look is the one that counts",
                         "are each set **exactly once**, as a plain `NAME=/path` line",
                         "it never prints a value from that file",
                         "nothing is mounted on or anywhere inside `uploads` or `vault`",
                         "No option makes the script accept such a database",
                         # Independent review, low 2.
                         "The state directory itself may not be a symbolic link either",
                         "everything is compared under the directory's real path"):
            self.assertIn(sentence, restore_section)
        # Independent review, low 1: the message the backup tool gives is the one the guide explains.
        backup_tool = (ROOT / "src" / "backup_application.py").read_text(encoding="utf-8")
        for said in ("the state changed hands while the snapshot was being made",
                     "a restore of this database began while the snapshot was being"):
            self.assertIn(said, " ".join(self.guide.split()))
            self.assertIn(said, backup_tool)
        self.assertIn("by device and inode number, not by name", " ".join(self.guide.split()))
        # Every way the script can end, as it prints it, is in the table of section 17.
        script = (ROOT / "deploy" / "learnpilot-restore.sh").read_text(encoding="utf-8")
        for ending in ("The database HAD BEEN REPLACED when the script was stopped",
                       "WHETHER THE DATABASE WAS REPLACED COULD NOT BE ESTABLISHED",
                       "INTERRUPTED WHILE REPLACING the live uploads and vault.",
                       "INTERRUPTED WHILE REPLACING the live state. The copy was NOT put in place",
                       "The restore itself is complete and the lock is released; the service is not running.",
                       "The service is running on the restored state: the script was stopped after it had started it.",
                       "Nothing was touched, and the service was left as it was."):
            self.assertIn(ending, script)
            self.assertIn(ending, restore_section)
        self.assertNotIn("only the start failed", self.guide)
        self.assertIn("No snapshot is taken while a restore is in progress or was left unfinished",
                      " ".join(self.guide.split()))
        # No command line anywhere in the guide invokes a path that releases from a log.
        for block in self.shell_blocks(self.guide):
            for gone in ("reconcile-partial", "reconcile-record", "--record-file", "lifecycle event="):
                self.assertNotIn(gone, block)
        self.assertNotRegex(self.guide, r"(?i)UPDATE quiz_assignments SET lifecycle_hold")

    def test_no_operator_command_is_documented_with_an_unquoted_scalar_expansion(self):
        # First Codex review, finding 8: AUDIT="--operator Gabriel Pilichev ..." followed by $AUDIT splits the name.
        for name in ("LIFECYCLE", "AUDIT"):
            self.assertNotRegex(self.guide, rf"\${name}\b", name)                 # $NAME
            self.assertNotRegex(self.guide, rf"(?m)^\s*{name}=\"", name)          # NAME="..."
            self.assertRegex(self.guide, rf"(?m)^\s*{name}=\(", name)             # NAME=( ... )
            self.assertIn(f'"${{{name}[@]}}"', self.guide)
            # Every expansion of the array is the quoted, element-preserving one.
            self.assertEqual(len(re.findall(rf"\$\{{{name}\b", self.guide)),
                             self.guide.count(f'"${{{name}[@]}}"'), name)
        self.assertRegex(self.guide, r'AUDIT=\(--operator "[^"]+ [^"]+" --audit-log ')  # a name of several words
        self.assertIn('--operator "Your Full Name" \\\n', self.restore)               # and to the script as well

    def test_the_documented_clock_commands_run_with_a_multiword_operator_name(self):
        incident = self.lifecycle.split("### Clock incident", 1)[1].split("### Restore", 1)[0]
        # As documented, without `set -e`: in an incident clock-status reports the fault with status 1.
        script = self.shell_blocks(incident)[0]
        now = lifecycle.format_timestamp(lifecycle.wall_reading())
        for documented, here in (
                ("cd /opt/learnpilot/app\n", ""),
                ("sudo -u learnpilot /opt/learnpilot/venv/bin/python", '"$PYTHON" -B'),
                ("/var/lib/learnpilot/data/questions.db", '"$DB"'),
                ("/var/lib/learnpilot/lifecycle-operator.log", '"$LOG"'),
                ('--verified-utc "2026-10-08 12:05:00"', f'--verified-utc "{now}"'),
                ('--suspect-from "2026-10-08 11:40:00" --suspect-to "2026-10-08 12:04:00"', ""),
                ('"Your Full Name"', '"Мария Иванова Петрова"')):
            self.assertIn(documented, script)
            script = script.replace(documented, here)
        with tempfile.TemporaryDirectory(prefix="lifecycle-guide-") as tmp:
            root = Path(tmp)
            database = build_test_database(root / "questions.db")
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("UPDATE lifecycle_clock SET state = 'suspect', state_reason = 'clock_step', "
                             "state_changed_at = ?, high_water_at = ?", (now, now))
                conn.commit()
            result = subprocess.run(
                ["bash", "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120,
                env={"PATH": os.environ.get("PATH", ""), "PYTHON": sys.executable, "DB": str(database),
                     "LOG": str(root / "operator.log"), "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entry = json.loads((root / "operator.log").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual((entry["action"], entry["operator"]), ("clock_recover", "Мария Иванова Петрова"))
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute("SELECT state FROM lifecycle_clock").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
