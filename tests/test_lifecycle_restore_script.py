"""The restore procedure as it is documented: ``deploy/learnpilot-restore.sh``.

The procedure is one script, and these tests run that script. What they are
there to show, review by review of PR #326:

* a failed step never leads to a start (second Codex review, blocker 4);
* an interrupted run is never completed from what it left on disk: there is no
  ``--complete-swap``, a forged or stale staging directory is never put in
  place, and the run after an interruption starts over, with every gate, against
  the live state as it then is (Claude review, H1);
* while a restore is unfinished nothing serves or backs up the database, after a
  reboot either;
* only a database that is demonstrably the later state of the restored one
  releases a hold (M1), and the clock is confirmed after reconciliation (M2).

Every run is isolated: a temporary state directory with a live database, uploads
and a vault, and a snapshot to restore. A directory of shims comes first in
``PATH``. ``sudo``, ``systemctl`` and ``curl`` are stand-ins. ``sqlite3``,
``tar``, ``install``, ``mv``, ``ln`` and the Python interpreter are the real
programs behind a wrapper that can be told to fail at one chosen call, or to
kill the script there, which is what a power cut looks like from the disk. The
operator CLI, the pre-flight checks and the migration runner are the
repository's own, run as the script runs them.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import src.backup_application as backup_module
from src import manage_migrations, restore_preflight
from src.backup_application import backup_application
from src.backup_sqlite import BackupError
from tests.support import ROOT, build_test_database
from tests.test_assignment_public_token_migration_sql import POPULATE_SQL
from web import production
from web.services import lifecycle

SCRIPT = ROOT / "deploy" / "learnpilot-restore.sh"
MIGRATIONS = ROOT / "web" / "migrations"
WRAPPED_TOOLS = ("sqlite3", "tar", "install", "mv", "ln")
NOT_STARTED = "THE SERVICE WAS NOT STARTED"
UNTOUCHED = "Nothing was touched, and the service was left as it was."
NOT_REPLACED = "Nothing was replaced: the live database, uploads and vault are exactly as they were."
OPERATOR = "Мария Иванова Петрова"  # three words: one argument all the way to the audit log
SECRET = "never-show-this-secret-value-of-the-service"

# Sourced by every shim. $RESTORE_RUN is the directory of one server: its call log, its
# counters, the files that stand for "the service is active" and "the timer is active",
# and the process id of the script. Several runs can work on one server at the same time:
# each has its own $RESTORE_TAG, and with it its own counters, call log and process id.
INJECT = r"""
run=${RESTORE_RUN:?}
tag=${RESTORE_TAG:-}
pause() {   # pause POINT: the run waits here, holding whatever it holds, until the test lets it go on
  : > "$run/paused$tag.$1"
  waited=0
  until [ -e "$run/resume$tag.$1" ]; do
    waited=$((waited + 1))
    if [ "$waited" -gt 2400 ]; then echo "shim: never let go on at $1" >&2; exit 98; fi
    sleep 0.05
  done
}
maybe_fail() {
  name=$1
  file="$run/count$tag.$(printf '%s' "$name" | tr -c 'A-Za-z0-9_.-' '_')"
  count=0
  if [ -f "$file" ]; then count=$(cat "$file"); fi
  count=$((count + 1))
  echo "$count" > "$file"
  echo "$name#$count" >> "$run/calls$tag.log"
  for spec in ${RESTORE_TEST_HOOK:-}; do      # something else happens just before this call
    if [ "${spec%%=*}" = "$name#$count" ]; then "${spec#*=}"; fi
  done
  for spec in ${RESTORE_TEST_PAUSE:-}; do     # the run waits just before this call does its work
    if [ "$spec" = "$name#$count" ]; then pause "$spec"; fi
  done
  for spec in ${RESTORE_TEST_KILL:-}; do
    if [ "$spec" = "$name#$count" ]; then
      kill -9 "$(cat "$run/script$tag.pid")"   # the script dies here, like the machine would
      exit 137
    fi
  done
  for spec in ${RESTORE_TEST_FAIL:-}; do
    if [ "${spec%%=*}" = "$name#$count" ]; then
      echo "shim: injected failure at $name#$count" >&2
      exit "${spec##*=}"
    fi
  done
  # What happens right after this call has done its work: a signal to the script, or an exit status.
  after=
  for spec in ${RESTORE_TEST_AFTER:-}; do
    if [ "${spec%%=*}" = "$name#$count" ]; then after=${spec##*=}; fi
  done
}

afterwards() {
  case $after in
    "") ;;
    TERM | INT | HUP)
      echo "shim: SIG$after to the script right after $name#$count" >&2
      kill -s "$after" "$(cat "$run/script$tag.pid")"
      ;;
    KILL)
      echo "shim: SIGKILL to the script right after $name#$count did its work" >&2
      kill -9 "$(cat "$run/script$tag.pid")"   # the script stops just after this call
      exit 137
      ;;
    KILLALL)
      # The machine stops just after this call: the script and everything it started, this shim
      # and the step it is part of included (each run is a process group of its own).
      echo "shim: SIGKILL to the whole run right after $name#$count did its work" >&2
      kill -9 -- "-$(cat "$run/script$tag.pid")"
      exit 137
      ;;
    PAUSE) pause "$name#$count" ;;
    *)
      echo "shim: $name#$count did its work and exits $after" >&2
      exit "$after"
      ;;
  esac
}
"""

SHIMS = {
    # Like sudo, it closes every descriptor from 3 up before it runs the command (sudo's closefrom): a
    # lock the caller holds is not handed on through it. Unlike sudo, it does not stay as its parent.
    "sudo": r"""
if [ "${1:-}" = "-u" ]; then shift 2; fi
for fd in /dev/fd/*; do
  fd=${fd##*/}
  case $fd in
    0 | 1 | 2) ;;
    *) eval "exec $fd>&-" 2>/dev/null ;;
  esac
done
exec "$@"
""",
    "systemctl": r"""
. "$RESTORE_SHIMS/inject.sh"
for unit in "$@"; do :; done
case $unit in
  *registration-cleanup*.timer) kind=cleanup-timer; flag=cleanup-timer-active ;;
  *registration-cleanup*.service) kind=cleanup; flag=cleanup-active ;;
  *backup*.timer) kind=timer; flag=timer-active ;;
  *backup*.service) kind=backup; flag=backup-active ;;
  *) kind=systemctl; flag=active ;;
esac
if [ "$1" = show ]; then              # show -p PROPERTY UNIT
  case $3 in
    ActiveState)
      if [ -f "$run/$kind-state" ]; then state=$(cat "$run/$kind-state")   # what a test says it is
      elif [ -e "$run/$flag" ]; then state=active
      else state=inactive; fi
      if [ "$state" = ERROR ]; then echo "Failed to connect to bus: No such file or directory" >&2; exit 1; fi
      echo "ActiveState=$state"
      ;;
    EnvironmentFiles) cat "$run/unit-environment-files" || exit 1 ;;
    Environment) cat "$run/unit-environment" || exit 1 ;;
    *) exit 1 ;;
  esac
  exit 0
fi
maybe_fail "$kind-$1"
case $1 in
  stop)
    if [ "$kind" != systemctl ] || [ -z "${RESTORE_TEST_STOP_DOES_NOTHING:-}" ]; then
      if [ -e "$run/$flag" ]; then /bin/mv "$run/$flag" "$run/$flag.off"; fi
    fi
    ;;
  start) : > "$run/$flag" ;;
esac
afterwards
""",
    # Records the call, and in flushed$tag.log what a call that was not failed was asked to flush; it
    # cannot show that Linux flushes anything.
    "sync": r"""
. "$RESTORE_SHIMS/inject.sh"
maybe_fail sync
printf '%s %s\n' "$name#$count" "$*" >> "$run/flushed$tag.log"
afterwards
""",
    "curl": r"""
. "$RESTORE_SHIMS/inject.sh"
maybe_fail curl
echo ok
""",
    # flock(1) as the script calls it: flock [-s|-x] [-n] DIRECTORY|FILE COMMAND [ARGUMENT...], or
    # flock [-s|-x] [-n] DESCRIPTOR. The guard (a directory) is logged as "flock#N" when it is asked
    # for and as "guard#N" once it is held; the lifetime lock (a file) as "life#N" before it is asked
    # for; a descriptor is not logged. util-linux flock(1) is used where it is installed (Linux);
    # elsewhere (macOS has none) flock.py takes the same flock(2) locks. The exclusion is the kernel's
    # either way; the behaviour of util-linux flock(1) itself is shown only where it is installed.
    "flock": r"""
. "$RESTORE_SHIMS/inject.sh"
options=
while [ "$#" -gt 0 ]; do
  case $1 in
    -s | -x | -n) options="$options $1"; shift ;;
    *) break ;;
  esac
done
target=$1
shift
real=
for candidate in /usr/bin/flock /bin/flock; do
  if [ -x "$candidate" ]; then real=$candidate; break; fi
done
if [ "$#" -gt 0 ] && [ -d "$target" ]; then
  maybe_fail flock
  afterwards
  set -- "$RESTORE_SHIMS/guarded" "$@"
elif [ "$#" -gt 0 ]; then
  maybe_fail life
  afterwards
fi
if [ -n "$real" ]; then exec "$real" $options "$target" "$@"; fi
exec "$RESTORE_REAL_PYTHON" -I "$RESTORE_SHIMS/flock.py" $options "$target" "$@"
""",
    # setpriv(1) as the script calls it: setpriv OPTION... -- COMMAND. The tests run as one user.
    "setpriv": r"""
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done
shift
exec "$@"
""",
    "guarded": r"""
. "$RESTORE_SHIMS/inject.sh"
maybe_fail guard
afterwards
exec "$@"
""",
    "ps": r"""
. "$RESTORE_SHIMS/inject.sh"
maybe_fail ps
exec /bin/ps "$@"
""",
    # The real rm, unless it is given the one path a test names: then it fails, and removes nothing.
    "rm": r"""
for arg in "$@"; do
  if [ -n "${RESTORE_TEST_RM_REFUSES:-}" ] && [ "$arg" = "$RESTORE_TEST_RM_REFUSES" ]; then
    echo "shim: rm refused for $arg" >&2
    exit 1
  fi
done
exec /bin/rm "$@"
""",
    "python": r"""
. "$RESTORE_SHIMS/inject.sh"
for arg in "$@"; do
  case $arg in
    -c) maybe_fail "py:check"; break ;;
    --fingerprint) maybe_fail "py:fingerprint"; break ;;
    --snapshot) maybe_fail "py:preflight"; break ;;
    --dry-run | --apply | restore-hold | restore-status | clock-recover | reconcile | verify \
      | registrations-restore-gate | registrations-restore-status)
      maybe_fail "py:$arg"
      if [ "$arg" = "${RESTORE_TEST_PRETEND:-}" ]; then exit 0; fi   # "succeeds" without having run
      break
      ;;
  esac
done
exec "$RESTORE_REAL_PYTHON" -B "$@"
""",
}
FLOCK_PY = '''"""flock(1) where util-linux is not installed.

flock [-s|-x] [-n] PATH COMMAND [ARGUMENT...]: flock(2) on PATH (a directory or a file), a lock that
the command inherits: it is held by the command and by everything it starts, until the last of them
has ended or died. flock [-s|-x] [-n] DESCRIPTOR: flock(2) on a descriptor this process inherited,
which stays locked for as long as its holder keeps it open. -n: 1 at once if it is taken."""
import fcntl
import os
import sys

arguments = sys.argv[1:]
mode, wait = fcntl.LOCK_EX, True
while arguments and arguments[0] in ("-s", "-x", "-n"):
    option = arguments.pop(0)
    if option == "-n":
        wait = False
    else:
        mode = fcntl.LOCK_SH if option == "-s" else fcntl.LOCK_EX
target, *command = arguments
flags = mode if wait else mode | fcntl.LOCK_NB
if target.isdigit() and not command:
    try:
        fcntl.flock(int(target), flags)
    except BlockingIOError:
        sys.exit(1)
    sys.exit(0)
descriptor = os.open(target, os.O_RDONLY)
try:
    fcntl.flock(descriptor, flags)
except BlockingIOError:
    sys.exit(1)
os.set_inheritable(descriptor, True)
os.execvp(command[0], command)
'''
HOLD = object()      # Restore.start(stdin=HOLD): the time is typed later, by the test
# The script under its own process id, recorded where the shims find it.
LAUNCH = 'echo $$ > "$RESTORE_RUN/script${RESTORE_TAG:-}.pid"; exec bash "$0" "$@"'
_shims: dict = {}


def shim_directory() -> Path:
    """The shims, written once each time the module is set up (they keep no state of their own)."""
    if "path" not in _shims:
        directory = tempfile.TemporaryDirectory(prefix="lifecycle-restore-shims-")
        # Forgotten when they are removed: the runner cleans the module up when a class of another
        # module comes next, and sets it up again when one of this module's follows that one.
        unittest.addModuleCleanup(_shims.clear)
        unittest.addModuleCleanup(directory.cleanup)
        path = Path(directory.name).resolve()
        (path / "inject.sh").write_text(INJECT, encoding="utf-8")
        (path / "flock.py").write_text(FLOCK_PY, encoding="utf-8")
        scripts = dict(SHIMS)
        for tool in WRAPPED_TOOLS:
            real = shutil.which(tool)
            if real is None:
                raise AssertionError(f"{tool} is needed to run the restore script")
            scripts[tool] = (f'\n. "$RESTORE_SHIMS/inject.sh"\nmaybe_fail {tool}\n'
                             f'if [ -z "$after" ]; then exec {real} "$@"; fi\n'
                             f'{real} "$@" || exit $?\nafterwards\n')
        for name, body in scripts.items():
            shim = path / name
            shim.write_text("#!/bin/bash" + body, encoding="utf-8")
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        _shims["path"] = path
        _shims["user"] = subprocess.check_output(["id", "-un"], text=True).strip()
        _shims["group"] = subprocess.check_output(["id", "-gn"], text=True).strip()
    return _shims["path"]


def setUpModule():
    shim_directory()


def utc(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%d %H:%M:%S")


def run_sql(path: Path, sql: str, params=()):
    with closing(sqlite3.connect(str(path))) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Restore:
    """One isolated server: live state, a snapshot, the shims, and what happened."""

    def __init__(self, cleanup, live_db: Path, snapshot_db: Path):
        directory = tempfile.TemporaryDirectory(prefix="lifecycle-restore-")
        cleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.state = self.root / "state"
        # The restore's own records (root's /var/lib/learnpilot-restore): the script makes it.
        self.records = self.root / "records"
        self.live_db = self.state / "data" / "questions.db"
        self.lock = Path(lifecycle.restore_lock_path(self.live_db))
        for path, text in (("data/reference/official.txt", "reference"),
                           ("uploads/practical/live.txt", "an upload made after the snapshot"),
                           ("vault/Generated/live.md", "a note written after the snapshot")):
            (self.state / path).parent.mkdir(parents=True, exist_ok=True)
            (self.state / path).write_text(text, encoding="utf-8")
        shutil.copyfile(live_db, self.live_db)
        # The service's settings, as systemd reports them and as its environment file holds them.
        self.env_file = self.root / "learnpilot.env"
        self.env_file.write_text(
            f"# LearnPilot production environment\nDZI_SECRET_KEY={SECRET}\nDZI_DB={self.live_db}\n"
            f"DZI_UPLOAD_ROOT={self.state}/uploads/practical\nDZI_VAULT={self.state}/vault\n"
            "DZI_PUBLIC_HOST=learnpilot.example\n", encoding="utf-8")
        self.backups = self.root / "backups"
        self.backups.mkdir()

        self.snapshot = self.root / "snapshot-20260101T000000Z"
        self.snapshot.mkdir()
        shutil.copyfile(snapshot_db, self.snapshot / "questions.db")
        tree = self.root / "archived"
        for path in ("uploads/practical/restored.txt", "vault/Generated/restored.md"):
            (tree / path).parent.mkdir(parents=True, exist_ok=True)
            (tree / path).write_text("restored", encoding="utf-8")
        with tarfile.open(self.snapshot / "files.tar.gz", "w:gz") as archive:
            archive.add(tree / "uploads", arcname="uploads")
            archive.add(tree / "vault", arcname="vault")

        self.shims = shim_directory()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        for flag in ("active", "timer-active", "cleanup-timer-active"):   # the service runs, and so do both timers
            (self.run_dir / flag).write_text("", encoding="utf-8")
        (self.run_dir / "unit-environment-files").write_text(
            f"EnvironmentFiles={self.env_file} (ignore_errors=no)\n", encoding="utf-8")
        (self.run_dir / "unit-environment").write_text(
            "Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\n", encoding="utf-8")
        # What was live when the restore began: none of it may ever be lost.
        self.original = self.live_files()

    # ------------------------------------------------------------------ running

    def run(self, *arguments, stdin=None, **options):
        return self.wait(self.start(*arguments, stdin=stdin, **options))

    def start(self, *arguments, tag="", fail="", kill="", after="", pause="", stdin=None, operator=OPERATOR,
              note="грешен ъпдейт на друга таблица", **extra_env):
        """The script, started and not waited for. Each run is a process group of its own (what
        ``KILLALL`` kills); runs on one server at the same time need tags of their own."""
        argv = ["bash", "-c", LAUNCH, str(SCRIPT), "--snapshot", str(self.snapshot), "--operator", operator]
        if "--replaced-database-unreadable" not in arguments and note is not None:
            argv += ["--evidence-note", note]
        for counter in self.run_dir.glob(f"count{tag}.*"):      # "mv#3" is the third mv of this run
            counter.unlink()
        environment = self.environment(fail, kill, RESTORE_TEST_AFTER=after, RESTORE_TEST_PAUSE=pause,
                                       RESTORE_TAG=tag, **extra_env)
        command = [*argv, *map(str, arguments)]
        if stdin is HOLD:     # nothing is typed until the test types it (type_the_time): the run waits there
            return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, cwd=self.root, start_new_session=True, env=environment)
        typed = self.root / f"typed{tag}"                        # what is typed when the script asks
        typed.write_text(utc() + "\n" if stdin is None else stdin, encoding="utf-8")
        with typed.open(encoding="utf-8") as given:
            return subprocess.Popen(command, stdin=given, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, cwd=self.root, start_new_session=True, env=environment)

    @staticmethod
    def type_the_time(process):
        process.stdin.write(utc() + "\n")
        process.stdin.close()

    @staticmethod
    def waits_for_input(process) -> bool:
        """The script itself is running and has no command running: it waits at its prompt."""
        children = subprocess.run(["pgrep", "-P", str(process.pid)], capture_output=True, text=True).stdout
        return process.poll() is None and not children.split()

    def wait(self, process, timeout=180):
        if process.stdin is not None and process.stdin.closed:
            process.stdin = None
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, 9)
            process.communicate()
            raise
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if SECRET in result.stdout + result.stderr:      # whatever a run prints, never a secret of the service
            raise AssertionError("the script printed a value from the service's environment file")
        return result

    def environment(self, fail="", kill="", **extra_env) -> dict:
        return {
            "PATH": f"{self.shims}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.root), "TMPDIR": str(self.root),
            "RESTORE_SHIMS": str(self.shims), "RESTORE_RUN": str(self.run_dir),
            "RESTORE_REAL_PYTHON": sys.executable, "RESTORE_TEST_FAIL": fail, "RESTORE_TEST_KILL": kill,
            "PYTHONDONTWRITEBYTECODE": "1",
            "LEARNPILOT_STATE": str(self.state), "LEARNPILOT_APP": str(ROOT),
            "LEARNPILOT_PYTHON": str(self.shims / "python"),
            "LEARNPILOT_USER": _shims["user"], "LEARNPILOT_GROUP": _shims["group"],
            "LEARNPILOT_RESTORE_RECORDS": str(self.records),
            **extra_env,
        }

    def run_as_documented(self, command_line: str, stdin=None):
        """A command line copied from the guide, typed into a shell as it stands."""
        for counter in self.run_dir.glob("count.*"):
            counter.unlink()
        return subprocess.run(["bash", "-c", command_line], input=utc() + "\n" if stdin is None else stdin,
                              capture_output=True, text=True, env=self.environment(), cwd=self.root, timeout=180,
                              start_new_session=True)

    # ------------------------------------------------------------------ runs at the same time

    def paused(self, tag: str, point: str) -> bool:
        return (self.run_dir / f"paused{tag}.{point}").exists()

    def go_on(self, tag: str, point: str):
        (self.run_dir / f"resume{tag}.{point}").write_text("", encoding="utf-8")

    @staticmethod
    def wait_until(condition, what: str, timeout: float = 90):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting until {what}")
            time.sleep(0.02)

    # ------------------------------------------------------------------ what happened

    def all_calls(self, tag: str = "") -> list[str]:
        log = self.run_dir / f"calls{tag}.log"
        return log.read_text(encoding="utf-8").split() if log.exists() else []

    def flushed(self, tag: str = "") -> dict[str, list[str]]:
        """Every sync that was carried out, by call ("sync#2"): the paths it was asked to flush."""
        log = self.run_dir / f"flushed{tag}.log"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return {call: [path for path in paths if path != "--"] for call, *paths in map(str.split, lines)}

    def calls(self) -> list[str]:
        """Every call but those of the registration cleanup unit, the flushes of the timer notes, the
        guard, the lifetime lock and the look at who holds the restore, which ``tests.test_registration_restore_operations``
        follows: the order of the rest is what the tests of this module pin, and those calls do not
        move it."""
        return [call for call in self.all_calls() if not call.startswith(("cleanup-", "sync#", "flock#", "guard#",
                                                                           "ps#", "life#"))]

    def started(self) -> bool:
        return any(call.startswith("systemctl-start#") for call in self.calls())

    def active(self) -> bool:
        return (self.run_dir / "active").exists()

    def timer_active(self) -> bool:
        return (self.run_dir / "timer-active").exists()

    def locked(self) -> bool:
        return lifecycle.restore_lock_present(self.live_db)

    def staging(self) -> list[Path]:
        return sorted(self.state.glob("restore-staging-*"))

    def replaced(self) -> list[Path]:
        return sorted(self.state.glob("replaced-*"))

    def live_files(self) -> dict:
        """Every file of the live state, by relative path: its bytes (SQLite sidecars left out)."""
        files = {}
        for path in sorted(self.state.rglob("*")):
            relative = path.relative_to(self.state).as_posix()
            if path.is_file() and not relative.startswith(("restore-staging-", "replaced-", "data/restore-in-progress")) \
                    and not relative.endswith(("-wal", "-shm", "lifecycle-operator.log")):
                files[relative] = path.read_bytes()
        return files

    def everything(self) -> set:
        """The content of every file under the state directory, wherever it now is."""
        return {path.read_bytes() for path in self.state.rglob("*") if path.is_file()}

    def holds(self) -> dict:
        return {int(row["id"]): row["lifecycle_hold"]
                for row in run_sql(self.live_db, "SELECT id, lifecycle_hold FROM quiz_assignments ORDER BY id")}

    def assignment(self, assignment_id):
        return run_sql(self.live_db, "SELECT * FROM quiz_assignments WHERE id = ?", (assignment_id,))[0]

    def audit(self) -> list[dict]:
        log = self.state / "lifecycle-operator.log"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def application_refuses(self) -> str | None:
        """What web.wsgi answers a start with (systemd starts an enabled unit after a reboot)."""
        return production.restore_lock_error({"DZI_DB": str(self.live_db)})

    def backup(self):
        return backup_application(self.live_db, self.state / "uploads" / "practical", self.state / "vault",
                                  self.backups)


class RestoreScriptTestCase(unittest.TestCase):
    """Three assignments. After the snapshot one was closed and one had its deadline extended."""

    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="lifecycle-restore-fixtures-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixtures = Path(directory.name)
        cls.snapshot_db = build_test_database(cls.fixtures / "snapshot.db")
        cls.section = run_sql(cls.snapshot_db, "SELECT MIN(id) AS id FROM curriculum_sections")[0]["id"]
        cls.earlier, cls.later = utc(timedelta(days=10))[:10] + " 21:00:00", utc(timedelta(days=20))[:10] + " 21:00:00"
        cls.closed_at = utc(timedelta(days=-1))
        ids = []
        for number, (title, deadline) in enumerate(
                (("Затворен след копието", None), ("Непроменен", None), ("Удължен", cls.earlier))):
            with closing(sqlite3.connect(str(cls.snapshot_db))) as conn:
                ids.append(conn.execute(
                    "INSERT INTO quiz_assignments (section_id, title_bg, question_count, created_at, public_token, "
                    "deadline_at, display_timezone) VALUES (?, ?, 1, ?, ?, ?, 'Europe/Sofia')",
                    (cls.section, title, utc(timedelta(days=-30 + number)), f"token-of-assignment-{number}",
                     deadline)).lastrowid)
                conn.commit()
        cls.closed, cls.unchanged, cls.extended = ids
        # The snapshot was taken yesterday: that is how far its lifecycle clock had run.
        cls.snapshot_clock, cls.live_clock = utc(timedelta(days=-1)), utc(timedelta(hours=-1))
        run_sql(cls.snapshot_db, "UPDATE lifecycle_clock SET high_water_at = ?", (cls.snapshot_clock,))
        # What happened after the snapshot, and is therefore only in the live database.
        cls.live_db = cls.fixtures / "live.db"
        shutil.copyfile(cls.snapshot_db, cls.live_db)
        run_sql(cls.live_db, "UPDATE quiz_assignments SET closed_at = ? WHERE id = ?", (cls.closed_at, cls.closed))
        run_sql(cls.live_db, "UPDATE quiz_assignments SET deadline_at = ? WHERE id = ?", (cls.later, cls.extended))
        run_sql(cls.live_db, "UPDATE lifecycle_clock SET high_water_at = ?", (cls.live_clock,))

    def server(self, live_db=None, snapshot_db=None) -> Restore:
        return Restore(self.addCleanup, live_db or self.live_db, snapshot_db or self.snapshot_db)

    def each(self, cases, check):
        """``check(case)`` for every case, as sub-tests. Each case runs the script on a server of
        its own, so several run at a time; what one of them raises is reported under its name."""
        def outcome(case):
            try:
                check(case)
            except BaseException as raised:   # an assertion, with its traceback
                return raised
            return None

        cases = list(cases)
        with ThreadPoolExecutor(max_workers=min(6, os.cpu_count() or 1)) as pool:
            outcomes = list(pool.map(outcome, cases))
        for case, raised in zip(cases, outcomes):
            with self.subTest(case=case):
                if raised is not None:
                    raise raised

    def variant(self, source: Path, name: str, sql: str = "", params=()) -> Path:
        path = self.fixtures / f"{self.id().rsplit('.', 1)[1]}-{name}.db"
        shutil.copyfile(source, path)
        if sql:
            run_sql(path, sql, params)
        return path

    def assert_not_started(self, server: Restore, result, *, status=None):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        if status is not None:
            self.assertEqual(result.returncode, status, result.stderr)
        self.assertFalse(server.started(), server.calls())
        self.assertFalse(server.active())
        self.assertIn(NOT_STARTED, result.stderr)
        self.assertNotIn("RESTORE COMPLETE", result.stderr)

    def assert_nothing_replaced(self, server: Restore, result, *, status=None):
        """Stopped after the service was stopped and before the database was replaced: the live state
        is what it was, the lock is gone and the backup timer is back, and the service stays stopped."""
        self.assert_not_started(server, result, status=status)
        self.assertIn(NOT_REPLACED, result.stderr)
        self.assertEqual((server.live_files(), server.staging(), server.replaced()), (server.original, [], []))
        self.assertEqual((server.locked(), server.timer_active()), (False, True))
        self.assertIsNone(server.application_refuses())

    def assert_untouched(self, server: Restore, result, *, status=None):
        """Given up in the pre-flight: nothing modified, nothing stopped, no lock."""
        if status is not None:
            self.assertEqual(result.returncode, status, result.stderr)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(UNTOUCHED, result.stderr)
        self.assertEqual((server.live_files(), server.staging(), server.replaced()), (server.original, [], []))
        self.assertEqual((server.active(), server.timer_active(), server.locked()), (True, True, False))
        self.assertFalse([call for call in server.calls() if call.split("-")[0] in ("systemctl", "timer")])

    def assert_restored(self, server: Restore, *, abandoned=0):
        """The live state is the snapshot's, with what the replaced database knew reconciled in,
        and nothing that was live has been lost: it is all still on the disk."""
        self.assertEqual((server.state / "uploads/practical/restored.txt").read_text(encoding="utf-8"), "restored")
        self.assertEqual((server.state / "vault/Generated/restored.md").read_text(encoding="utf-8"), "restored")
        self.assertFalse((server.state / "uploads/practical/live.txt").exists())
        self.assertEqual((server.state / "data/reference/official.txt").read_text(encoding="utf-8"), "reference")
        self.assertEqual(len(server.staging()), abandoned)
        self.assertEqual(run_sql(server.live_db, "PRAGMA quick_check")[0][0], "ok")
        self.assertEqual((server.locked(), server.active(), server.timer_active()), (False, True, True))
        self.assertIsNone(server.application_refuses())
        kept = server.everything()
        for relative, content in server.original.items():
            self.assertIn(content, kept, f"{relative} as it was live is no longer anywhere")


class ShimLifecycleTest(unittest.TestCase):
    """Follow-up to the independent review of PR #326. The shims were removed with the module and
    still remembered when it was set up again, which the runner does when a class of another module
    ran in between. Every restore after that ran without them: "a program this script needs is not
    installed: systemctl"."""

    def test_the_shims_are_written_again_when_the_module_is_set_up_again(self):
        self.addCleanup(shim_directory)             # however this ends, the classes after it have shims
        first = shim_directory()
        self.assertTrue((first / "inject.sh").is_file())
        unittest.doModuleCleanups()                 # the runner, when a class of another module comes next
        self.assertFalse(first.exists())
        setUpModule()                               # and when one of this module's follows that one
        again = shim_directory()
        self.assertNotEqual(again, first)
        self.assertTrue((again / "inject.sh").is_file())
        for program in (*SHIMS, *WRAPPED_TOOLS):   # each where a server's PATH has it first
            self.assertEqual(shutil.which(program, path=str(again)), str(again / program))


class RestoreScriptSourceTest(unittest.TestCase):
    """What the script's text must and must not contain."""

    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")
        head, rest = cls.source.split("<<'USAGE'\n", 1)
        without_usage_text = head + rest.split("\nUSAGE\n", 1)[1]
        cls.code = [line for line in without_usage_text.splitlines() if not line.lstrip().startswith("#")]
        cls.text = "\n".join(cls.code)

    def test_it_parses_and_fails_fast(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True).returncode, 0)
        self.assertIn("set -Eeuo pipefail", self.code)
        for forbidden in ("|| true", "|| :", "set +e", "set +o errexit", "set +o pipefail", "|| echo", "2>/dev/null"):
            self.assertNotIn(forbidden, self.text, forbidden)

    def test_the_service_is_started_by_exactly_one_command_after_every_gate(self):
        starts = [number for number, line in enumerate(self.code) if 'systemctl start "$SERVICE"' in line]
        self.assertEqual(len(starts), 1, "exactly one start of the service")
        (start,) = starts
        self.assertEqual(self.code[start], 'if ! "${AS_OWNER[@]}" systemctl start "$SERVICE"; then')  # not in a branch or a function
        # The one other `systemctl start` is of a timer: the step under the guard (RESTART) that
        # restart_timers runs for the backup timer and for the registration cleanup timer, once
        # each; never of the service. restart_timers is run by the pre-flight (notes an earlier run
        # left), by the end of a successful run and by finish (lock given back; restore complete
        # without a start; stopped after the start).
        others = [line.strip() for line in self.code if re.search(r"systemctl\s+start\b", line) and "$SERVICE" not in line]
        self.assertEqual(others, ['if ! systemctl start "$6"; then'])
        restarts = re.findall(r'sudo flock "\$RECORDS" sh -c "\$CHECKED\$RESTART" sh "\$RECORDS" "\$LOCK" '
                              r'"\$OWNER_RECORD" "\$SWAP_RECORD"[\s\\]+"\$(\w+)"[\s\\]+'
                              r'"\$(\w+)" "\$1"', self.text)
        self.assertEqual(restarts, [("TIMER_NOTE", "BACKUP_TIMER"), ("CLEANUP_TIMER_NOTE", "CLEANUP_TIMER")])
        self.assertEqual(len(re.findall(r"if ! restart_timers flagged; then", self.text)), 4)
        self.assertEqual(len(re.findall(r"if ! restart_timers noted; then", self.text)), 1)
        later = "\n".join(self.code[start + 1:])
        self.assertNotRegex(later, r"(?m)^\s*gate\s")           # nothing mandatory is left for after the start
        self.assertNotIn("-m src.", later)                      # no tool is run after it, only named
        before = "\n".join(self.code[:start])
        order = []
        for marker in (
                "-m src.restore_preflight --snapshot",
                'sudo flock "$RECORDS" sh -c "$CHECKED$ACQUIRE"',
                '"$TIMER_NOTE" "$BACKUP_TIMER" "$STOP_IT"',
                'gate "stop the service" "${AS_OWNER[@]}" systemctl stop "$SERVICE"',
                '"$SNAPSHOT/questions.db" "$STAGED_DB"',
                '-xzf "$SNAPSHOT/files.tar.gz"',
                '"${MIGRATE[@]}" --apply',
                '"${REGISTRATIONS[@]}" registrations-restore-gate',
                'restore-hold --pre-restore-db "$LIVE_DB"',
                "restore-status --expect-held all",
                'reconcile --pre-restore-db "$LIVE_DB" --snapshot-db "$SNAPSHOT/questions.db"',
                '"${LIFECYCLE[@]}" "${RECOVER[@]}" --verified-utc "$VERIFIED_UTC"',
                '"${LIFECYCLE[@]}" verify',
                '"${REGISTRATIONS[@]}" registrations-restore-status --expect held',
                '"${LIFECYCLE[@]}" restore-status --expect-held "$ACCEPT_HELD"',
                'gate "put the restored $tree in place"',
                '"${AS_OWNER[@]}" ln -- "$LIVE_DB" "$REPLACED/questions.db"',
                '"${AS_OWNER[@]}" mv -f -- "$STAGED_DB" "$LIVE_DB"',
                '"${IN_PLACE[@]}" verify',
                '--db "$LIVE_DB" registrations-restore-status --expect held',
                '"${IN_PLACE[@]}" restore-status --expect-held "$ACCEPT_HELD"',
                'gate "release the restore lock"'):
            self.assertEqual(before.count(marker), 1, marker)
            order.append(before.index(marker))
        self.assertEqual(order, sorted(order))

    def test_no_mandatory_command_can_hide_its_failure(self):
        functions = set(re.findall(r"(?m)^(\w+)\(\) \{", self.source))
        self.assertEqual(functions, {"say", "usage", "die", "gate", "finish", "restart_timers", "timers_left", "outcome"})
        gates = re.findall(r'(?m)^\s*gate "[^"]*"(?: \\\n\s*| )(\S+)', self.source)
        self.assertGreater(len(gates), 30)
        # A gate runs an external program (or the builtin cd or test), never one of the script's own functions.
        self.assertEqual(set(gates) - {"sudo", "cd", "test", '"${AS_OWNER[@]}"', '"${AS_SERVICE[@]}"', '"${MIGRATE[@]}"',
                                       '"${LIFECYCLE[@]}"', '"${IN_PLACE[@]}"', '"${FINGERPRINT[@]}"',
                                       '"${REGISTRATIONS[@]}"'}, set())
        # No pipeline anywhere: outside quotes and ${...}, the only "|" left after "||" is the
        # alternation of a case pattern.
        for line in self.code:
            rest = re.sub(r'"[^"]*"|\$\{[^}]*\}', "X", line).replace("||", "")
            if "|" in rest:
                self.assertRegex(rest, r"^\s*(\S+ \| )+\S+\)", line)
        # The lifecycle and migration commands are run only through a gate or with their status read.
        direct = 0
        for number, line in enumerate(self.code):
            if re.match(r'\s*"\$\{(LIFECYCLE|MIGRATE|IN_PLACE)\[@\]\}"', line):
                previous = self.code[number - 1].strip()
                if previous.startswith('gate "') and previous.endswith("\\"):
                    continue
                direct += 1
                following = " ".join(self.code[number:number + 2])
                self.assertRegex(following, r"\|\| (RECONCILED|RECOVERED|HELD)=\$\?", line)
        self.assertEqual(direct, 3)  # reconcile (0 or 3), clock-recover (asked again), the count of what is held
        self.assertNotRegex(self.source, r"(?m)^\s*local \w+=\$\(")   # `local x=$(cmd)` discards cmd's status

    def test_every_array_and_everything_an_operator_types_is_expanded_quoted(self):
        for name in ("AS_SERVICE", "AUDIT", "MIGRATE", "LIFECYCLE", "IN_PLACE", "RECOVER", "FINGERPRINT"):
            self.assertRegex(self.source, rf"(?m)^\s*{name}=\(")
            self.assertEqual(len(re.findall(rf"\$\{{{name}\b", self.source)),
                             self.source.count(f'"${{{name}[@]}}"'), name)
        self.assertIn('AUDIT=(--operator "$OPERATOR" --audit-log "$AUDIT_LOG")', self.source)
        # One word wherever it is used: inside double quotes, or the word of a `case` (never split).
        uses = 0
        names = "OPERATOR|EVIDENCE_NOTE|VERIFIED_UTC|SUSPECT_FROM|SUSPECT_TO|SNAPSHOT|LIVE_DB|STAGED_DB|LOCK|STAGE|REPLACED"
        for line in self.code:
            for match in re.finditer(rf"\$({names})\b", line):
                uses += 1
                quoted = line[:match.start()].count('"') % 2 == 1
                assignment = re.fullmatch(r"\s*[A-Z_]+=\S+", line) is not None   # not split either
                self.assertTrue(quoted or assignment or line.strip().startswith("case "), line)
        self.assertGreater(uses, 40)

    def test_nothing_on_disk_is_taken_as_proof_that_a_check_was_made(self):
        # Claude review, H1: `--complete-swap` accepted a hand-made staging directory and marker.
        self.assertNotIn("gates-passed", self.source)
        mentions = [number for number, line in enumerate(self.code) if "complete-swap" in line]
        self.assertEqual(len(mentions), 2)                       # the option, and the sentence that refuses it
        self.assertEqual(self.code[mentions[0]].strip(), "--complete-swap)")
        self.assertTrue(self.code[mentions[1]].strip().startswith('die 2 "--complete-swap does not exist any more.'))
        # What the script reads back from its lock: whether a replacement had begun, and, from a
        # lock an earlier version of this script left, whether a timer has to be started again (now
        # noted in the restore's own records). Who holds the lock is recorded there too, not in the
        # lock, which sits in a directory the service can write. None of it lets a step be skipped.
        self.assertEqual(re.findall(r'"\$LOCK/', self.text), [])
        # Inside it, only in the step that takes the restore, and only to see what an earlier version
        # left there: each such mark is written into the records before anything else.
        self.assertEqual(sorted(set(re.findall(r'"\$lock/([a-z-]+)"', self.text))),
                         ["backup-timer-was-active", "registration-cleanup-timer-was-active", "swap-started"])
        self.assertEqual(self.text.count("INTERRUPTED_BEFORE=yes"), 1)
        for use in re.findall(r".*INTERRUPTED_BEFORE.*", self.text):
            self.assertRegex(use, r'INTERRUPTED_BEFORE=(no|yes)$|\[ "\$INTERRUPTED_BEFORE" = yes \]')
        # A run only ever works on the copy it made itself, under its own stamp.
        self.assertEqual(self.text.count("STAMP=$(date -u +%Y%m%dT%H%M%SZ)"), 2)   # taken, and taken again
        self.assertIn('while sudo test -e "$STATE/restore-staging-$STAMP" || sudo test -e "$STATE/replaced-$STAMP"; do',
                      self.text)                                               # if it is another run's
        self.assertEqual(self.text.count("STAGE=$STATE/restore-staging-$STAMP"), 1)
        self.assertEqual(self.text.count("restore-staging-*"), 2)   # listing what was left behind; the guard of rm
        self.assertEqual(self.text.count("rm -rf --"), 3)           # this run's copy, and the lock (RELEASE, twice)
        # The service's settings are asked of systemd, and nothing of them is ever echoed.
        for asked in ('show -p EnvironmentFiles "$SERVICE"', 'show -p Environment "$SERVICE"',
                      'show -p ActiveState "$BACKUP_SERVICE"', 'show -p ActiveState "$SERVICE"'):
            self.assertIn(asked, self.text)
        self.assertNotIn("is-active", self.text)
        for shown in re.findall(r'(?:say|die [0-9$?"]+) "[^\n]*', self.text):
            self.assertNotRegex(shown, r"\$\{?UNIT_SETTINGS|\$\(sudo cat")
        self.assertIn(f"LOCK=$STATE/data/{lifecycle.RESTORE_LOCK_NAME}", self.source)

    def test_the_database_is_replaced_last_and_by_one_rename(self):
        flip = 'gate "replace the live database by the copy" "${AS_OWNER[@]}" mv -f -- "$STAGED_DB" "$LIVE_DB"'
        self.assertEqual(self.text.count(flip), 1)
        position = self.text.index(flip)
        for earlier in ('gate "put the restored $tree in place"', "PRAGMA wal_checkpoint(TRUNCATE);",
                        'gate "keep the database being replaced" "${AS_OWNER[@]}" ln -- "$LIVE_DB" "$REPLACED/questions.db"',
                        '"${LIFECYCLE[@]}" restore-status --expect-held "$ACCEPT_HELD"'):
            self.assertLess(self.text.index(earlier), position, earlier)
        # After it nothing changes the live state any more: two read-only checks, the lock, the start.
        rest = self.text[position:].split("PHASE=committed", 1)[1].split('gate "release the restore lock"', 1)[0]
        self.assertNotRegex(rest, r"\b(mv|ln|install|tar|sqlite3)\b")
        # And until it, the live database is only read, checkpointed and given a second name.
        untouched = self.text[:position]
        self.assertNotRegex(untouched, r'\b(mv|rm)\b[^\n]*"\$LIVE_DB"(?!\$)')

    def test_what_is_taken_back_is_decided_by_what_is_on_the_disk(self):
        # Claude review, N1. A step can be cut off after its rename and before the script has noted
        # anything: the phase variable does not say whether the database was replaced.
        finish = self.text.split("finish() {", 1)[1].split("\ntrap finish EXIT", 1)[0]
        swap = finish.split('if [ "$PHASE" = swap ]; then', 1)[1].split("case $PHASE in", 1)[0]
        # First it looks: what is at the live path, to the byte.
        look = [swap.index(step) for step in (
            '"${FINGERPRINT[@]}" "$LIVE_DB" --expect "$VERIFIED"', '"${FINGERPRINT[@]}" "$STAGED_DB" --expect "$VERIFIED"',
            "case $at_live in", '"${AS_OWNER[@]}" sh -c "$UNMOVE"')]
        self.assertEqual(look, sorted(look))
        # The copy is there: committed, and nothing is moved or removed on that path.
        copy = swap.split("      copy)", 1)[1].split("      original)", 1)[0]
        self.assertIn("PHASE=committed", copy)
        self.assertNotRegex(copy, r"\b(mv|rm|sudo|AS_OWNER)\b")
        # Neither: nothing is moved back either, and the phase stays what keeps the lock.
        neither = swap.split("      *)\n        say", 1)[1]
        self.assertNotRegex(neither, r"\b(mv|rm|sudo|AS_OWNER)\b|PHASE=")
        # "Nothing was replaced" is said in one place, reached only as `locked`.
        self.assertEqual(finish.count("Nothing was replaced"), 1)
        self.assertEqual(finish.count("PHASE=locked"), 1)
        # The record of the replacement goes only once this run has taken its own replacement back.
        self.assertLess(finish.index('if [ "$INTERRUPTED_BEFORE" = yes ] \\\n'
                                     '            || sudo flock "$RECORDS" sh -c "$CHECKED$SWAP" sh "$RECORDS" '
                                     '"$OWNER_RECORD" "$$" "$SWAP_RECORD" clear; then'),
                        finish.index("PHASE=locked"))
        # No file is removed on the way back except a second name of the live database, checked as one.
        self.assertIn('if [ "$1" -ef "$2" ]; then\n  rm -f -- "$2"', self.source)
        # That, and the copy's empty log; a timer's note, after its timer started (RESTART); the record of
        # a replacement this run took back itself (SWAP); the records of the restore when it is given
        # back (RELEASE, three ways).
        self.assertEqual(len(re.findall(r"\brm -f -- ", self.text)), 7)
        # Every step is written down before it is made, in one assignment.
        for step in re.findall(r'(?m)^ *UNDO\+=\(.*\)\n(.*)', self.source):
            self.assertRegex(step, r'^\s*gate "')
        self.assertEqual(self.source.count("UNDO+=("), 5)
        # The flip itself is not on that list: it is the one thing judged by looking.
        self.assertNotRegex(self.source, r'UNDO\+=\([^\n]*\$STAGED_DB')
        # Signals: each ends the script through the same handler, and none cuts the handler short.
        for signal, status in (("HUP", 129), ("INT", 130), ("QUIT", 131), ("TERM", 143)):
            self.assertRegex(self.text, rf"trap 'FAILURE=\"interrupted \(SIG{signal}\)\"; exit {status}' {signal}")
        self.assertIn("trap '' HUP INT QUIT TERM", finish)
        self.assertIn("trap '' PIPE", self.text)


class PreflightTest(unittest.TestCase):
    """``python3 -m src.restore_preflight``: what would stop a restore half-way is found before it begins."""

    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="lifecycle-preflight-fixture-")
        cls.addClassCleanup(directory.cleanup)
        cls.database = build_test_database(Path(directory.name) / "questions.db")

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lifecycle-preflight-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.snapshot, self.state = self.root / "snapshot", self.root / "state"
        self.snapshot.mkdir()
        (self.state / "data").mkdir(parents=True)
        (self.state / "uploads").mkdir()
        shutil.copyfile(self.database, self.snapshot / "questions.db")
        self.archive(("uploads/practical/a.txt", b"x" * 1000), ("vault/Generated/n.md", b"note"))

    def archive(self, *members, name="files.tar.gz"):
        with tarfile.open(self.snapshot / name, "w:gz") as archive:
            for member in members:
                if isinstance(member, tarfile.TarInfo):
                    archive.addfile(member)
                    continue
                path, content = member
                info = tarfile.TarInfo(path)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

    def check(self):
        out = io.StringIO()
        return restore_preflight.preflight(self.snapshot, self.state, out), out.getvalue()

    def test_a_complete_snapshot_and_a_usable_state_directory_pass(self):
        before = {path: path.stat().st_mtime_ns for path in self.root.rglob("*")}
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("OK: the snapshot is complete and readable", out)
        self.assertRegex(out, r"space for the copy: about \d+ MiB needed, \d+ MiB free")
        self.assertEqual({path: path.stat().st_mtime_ns for path in self.root.rglob("*")}, before)  # read-only
        self.assertEqual(restore_preflight.main(
            ["--snapshot", str(self.snapshot), "--state-root", str(self.state)], out=io.StringIO()), 0)
        with self.assertRaises(SystemExit):
            restore_preflight.main(["--snapshot", str(self.snapshot)], out=io.StringIO())

    def test_the_snapshot_database_has_to_be_there_and_sound(self):
        cases = {"it is missing": None, "it is not a database": b"not a database" * 200,
                 "it is cut short": self.database.read_bytes()[:40000]}
        for label, content in cases.items():
            with self.subTest(database=label):
                (self.snapshot / "questions.db").unlink(missing_ok=True)
                if content is not None:
                    (self.snapshot / "questions.db").write_bytes(content)
                code, out = self.check()
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL: the snapshot", out)
        # A sound SQLite file that is not this application's database.
        (self.snapshot / "questions.db").unlink()
        sqlite3.connect(self.snapshot / "questions.db").close()
        self.assertEqual(self.check()[0], 1)

    def test_the_archive_has_to_be_readable_to_its_end_and_hold_only_what_a_snapshot_holds(self):
        link = tarfile.TarInfo("uploads/practical/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        device = tarfile.TarInfo("vault/device")
        device.type = tarfile.CHRTYPE
        bad_members = {
            "an absolute path": ("/etc/cron.d/x", b"x"),
            "a path that climbs out": ("uploads/../../outside", b"x"),
            "another tree": ("data/questions.db", b"x"),
            "a symbolic link": link,
            "a device": device,
        }
        for label, member in bad_members.items():
            with self.subTest(member=label):
                self.archive(("uploads/practical/a.txt", b"x"), member)
                code, out = self.check()
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL: the archive has an entry", out)
        whole = (self.snapshot / "files.tar.gz").read_bytes()
        for label, content in (("cut short", whole[:len(whole) // 2]), ("not an archive", b"plain text"), ("gone", None)):
            with self.subTest(archive=label):
                (self.snapshot / "files.tar.gz").unlink(missing_ok=True)
                if content is not None:
                    (self.snapshot / "files.tar.gz").write_bytes(content)
                code, out = self.check()
                self.assertEqual(code, 1, out)

    def test_the_state_directory_has_to_be_writable_on_one_filesystem_with_room(self):
        shutil.rmtree(self.state / "data")
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: not a directory: {self.state / 'data'}", out)
        (self.state / "data").mkdir()

        if os.geteuid() != 0:
            (self.state / "data").chmod(0o500)
            self.addCleanup((self.state / "data").chmod, 0o700)
            code, out = self.check()
            self.assertEqual(code, 1)
            self.assertIn("FAIL: not writable by this user", out)
            (self.state / "data").chmod(0o700)

        with mock.patch.object(restore_preflight, "_device", side_effect=lambda path: 2 if path.name == "uploads" else 1):
            code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: a filesystem is mounted at {self.state / 'uploads'}", out)

        usage = namedtuple("usage", "total used free")
        with mock.patch.object(restore_preflight.shutil, "disk_usage", return_value=usage(10**12, 10**12, 30 * 2**20)):
            code, out = self.check()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL: not enough free space in .* for a copy of the snapshot: about \d+ MiB needed, 30 MiB free")
        self.assertEqual(self.check()[0], 0)


    def test_nothing_is_mounted_or_linked_inside_what_is_replaced_by_renaming(self):
        # Claude review, N5: only the top-level directories were compared. `uploads` is renamed as a
        # whole, so a volume mounted at uploads/practical would be set aside with it, and the restored
        # files written to the state directory's own disk.
        uploads, vault, data = (self.state / name for name in ("uploads", "vault", "data"))
        practical, deep, generated, reference = (uploads / "practical", uploads / "practical" / "2026" / "class-12",
                                                 vault / "Generated", data / "reference")
        for directory in (deep, generated, reference):
            directory.mkdir(parents=True)
        self.assertEqual(self.check()[0], 0)

        def patched(**replacements):
            patches = [mock.patch.object(restore_preflight, name, value) for name, value in replacements.items()]
            for patch in patches:
                patch.start()
                self.addCleanup(patch.stop)
            try:
                return self.check()
            finally:
                for patch in patches:
                    patch.stop()

        real_device = restore_preflight._device
        cases = {
            "uploads/practical is a volume of its own": (
                dict(_is_mount=lambda path: path == practical),
                f"a filesystem is mounted at {practical}: the restore replaces uploads as a whole, by renaming it"),
            "a directory deep inside is on another device": (
                dict(_device=lambda path: 99 if path == deep else real_device(path)),
                f"a filesystem is mounted at {deep}: the restore replaces uploads as a whole"),
            "uploads itself is a mount point": (
                dict(_is_mount=lambda path: path == uploads), f"a filesystem is mounted at {uploads}:"),
            "a bind mount that only the kernel's list shows": (
                dict(_mount_points=lambda: [Path("/"), generated]),
                f"a filesystem is mounted at {generated}, inside what the restore replaces by renaming"),
            "the data directory is on another filesystem": (
                dict(_device=lambda path: 99 if path == data else real_device(path)),
                f"{data} is a symbolic link or on another filesystem"),
        }
        for label, (replacements, message) in cases.items():
            with self.subTest(layout=label):
                code, out = patched(**replacements)
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL: " + message, out)
        # Reference data is not moved: it may be a filesystem of its own.
        code, out = patched(_is_mount=lambda path: path == reference, _mount_points=lambda: [reference, Path("/")])
        self.assertEqual(code, 0, out)

        # Symbolic links, for real: renaming would replace the link and leave what it points to.
        elsewhere = self.root / "a-volume"
        practical.rename(elsewhere)
        practical.symlink_to(elsewhere)
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: {practical} is a symbolic link: the restore replaces uploads as a whole", out)
        practical.unlink()
        elsewhere.rename(practical)
        (data / "questions.db").symlink_to(self.snapshot / "questions.db")
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: {data / 'questions.db'} is a symbolic link", out)
        (data / "questions.db").unlink()
        self.assertEqual(self.check()[0], 0)

    def test_the_kernels_list_of_mount_points_is_read_with_its_escapes(self):
        listing = self.root / "mountinfo"
        listing.write_text(
            "25 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
            "88 25 8:17 / /var/lib/learnpilot/uploads/practical rw,relatime shared:44 - ext4 /dev/sdb1 rw\n"
            "91 25 8:1 /srv/a\\040b /var/lib/learnpilot/vault/My\\040Notes rw - ext4 /dev/sda1 rw\n"
            "short line\n", encoding="utf-8")
        self.assertEqual(restore_preflight._mount_points(str(listing)),
                         [Path("/"), Path("/var/lib/learnpilot/uploads/practical"),
                          Path("/var/lib/learnpilot/vault/My Notes")])
        self.assertEqual(restore_preflight._mount_points(str(self.root / "no-such-list")), [])


    def preflight_with(self, state, **replacements):
        out = io.StringIO()
        if not replacements:
            return restore_preflight.preflight(self.snapshot, state, out), out.getvalue()
        with mock.patch.multiple(restore_preflight, **replacements):
            return restore_preflight.preflight(self.snapshot, state, out), out.getvalue()

    def test_a_state_directory_that_is_a_symbolic_link_is_refused_as_it_stands(self):
        # Independent review of PR #326, low 2. The kernel lists mount points under their real
        # paths; they were compared with paths under the name that was given. With the state
        # directory a link, a filesystem bind-mounted inside vault was never found.
        real = self.root / "volume" / "learnpilot"
        real.parent.mkdir()
        self.state.rename(real)
        self.state.symlink_to(real)
        generated = real / "vault" / "Generated"
        generated.mkdir(parents=True)
        refusal = f"FAIL: the state directory {self.state} is a symbolic link (to {real})"

        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn(refusal, out)
        self.assertEqual(out.count("FAIL:"), 1)
        # Refused as it stands: nothing behind the link is looked at, mounted or not.
        asked = []
        code, out = self.preflight_with(
            self.state, _mount_points=lambda: asked.append("the list") or [generated],
            _is_mount=lambda path: asked.append(path) or False, _device=lambda path: asked.append(path) or 1)
        self.assertEqual((code, out.count("FAIL:"), asked), (1, 1, []))
        self.assertIn(refusal, out)
        self.assertEqual(restore_preflight.nested_filesystem_problems(self.state), [refusal[len("FAIL: "):]
                         + ": what is mounted inside it cannot be told under that name. Give the directory itself "
                         "as the state directory, in the service's settings and to the restore, or make it a real "
                         "directory"])
        # And before anything else is said about what it points to: with no data directory behind
        # the link, the refusal is still the link, and the only one.
        (real / "data").rename(real / "data-moved-away")
        code, out = self.check()
        self.assertEqual((code, out.count("FAIL:")), (1, 1))
        self.assertIn(refusal, out)
        (real / "data-moved-away").rename(real / "data")
        # However the link is written: with a trailing slash, or relative to where it is.
        out = io.StringIO()
        self.assertEqual(restore_preflight.main(["--snapshot", str(self.snapshot), "--state-root", f"{self.state}/"],
                                                out=out), 1)
        self.assertIn(refusal, out.getvalue())
        self.state.unlink()
        self.state.symlink_to(Path("volume") / "learnpilot")
        code, out = self.check()
        self.assertEqual((code, out.count("FAIL:")), (1, 1))
        self.assertIn(f"FAIL: the state directory {self.state} is a symbolic link (to {real})", out)

        # The directory itself, under its own name, is checked as any other: it passes, and what
        # is mounted inside it is found.
        self.assertEqual(self.preflight_with(real)[0], 0)
        code, out = self.preflight_with(real, _mount_points=lambda: [Path("/"), generated])
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: a filesystem is mounted at {generated}, inside what the restore replaces by renaming", out)

    def test_a_link_above_the_state_directory_hides_nothing_that_is_mounted_inside(self):
        # /var/lib on another disk, say: the state directory is a real directory, reached through a
        # link above it. Every comparison is made on its real path, the kernel's.
        disk = self.root / "disk"
        disk.mkdir()
        self.state.rename(disk / "state")
        (self.root / "lib").symlink_to(disk)
        named, real = self.root / "lib" / "state", disk / "state"
        practical, generated, reference = real / "uploads" / "practical", real / "vault" / "Generated", real / "data" / "reference"
        for directory in (practical / "2026", generated, reference):
            directory.mkdir(parents=True)
        self.assertFalse(named.is_symlink())
        self.assertNotEqual(named, real)
        code, out = self.preflight_with(named)
        self.assertEqual(code, 0, out)

        asked = []
        real_device = restore_preflight._device

        def mounted_at(place):
            return lambda path: asked.append(path) or path == place

        def another_device_at(place):
            return lambda path: asked.append(path) or (99 if path == place else real_device(path))

        cases = {
            "a bind mount that only the kernel's list shows": (
                dict(_mount_points=lambda: [Path("/"), generated]),
                f"a filesystem is mounted at {generated}, inside what the restore replaces by renaming"),
            "a bind mount on uploads itself": (
                dict(_mount_points=lambda: [real / "uploads"]),
                f"a filesystem is mounted at {real / 'uploads'}, inside what the restore replaces by renaming"),
            "a volume at uploads/practical": (
                dict(_is_mount=mounted_at(practical)),
                f"a filesystem is mounted at {practical}: the restore replaces uploads as a whole"),
            "another device deep inside": (
                dict(_device=another_device_at(practical / "2026")),
                f"a filesystem is mounted at {practical / '2026'}: the restore replaces uploads as a whole"),
            "the data directory on another filesystem": (
                dict(_device=another_device_at(real / "data")),
                f"{real / 'data'} is a symbolic link or on another filesystem"),
        }
        for label, (replacements, message) in cases.items():
            with self.subTest(layout=label):
                code, out = self.preflight_with(named, **replacements)
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL: " + message, out)
        # Reference data is not moved and may be a filesystem of its own, here as anywhere.
        code, out = self.preflight_with(named, _is_mount=mounted_at(reference),
                                        _mount_points=lambda: [reference, Path("/")])
        self.assertEqual(code, 0, out)
        # Somebody else's mount points are nobody's problem here.
        code, out = self.preflight_with(named, _mount_points=lambda: [disk / "another-state" / "vault", disk])
        self.assertEqual(code, 0, out)
        # Nothing was asked under the name that goes through the link: one spelling throughout.
        self.assertGreater(len(asked), 10)
        for path in asked:
            self.assertTrue(path == real or real in path.parents, path)
        # A link inside what is renamed is still a link, and is not followed to make it "real".
        elsewhere = self.root / "a-volume"
        practical.rename(elsewhere)
        practical.symlink_to(elsewhere)
        code, out = self.preflight_with(named)
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: {practical} is a symbolic link: the restore replaces uploads as a whole", out)


class RestoreScriptFlowTest(RestoreScriptTestCase):
    """The procedure with the real operator CLI, start to finish."""

    def test_a_restore_reconciles_from_the_replaced_database_and_only_then_starts(self):
        server = self.server()
        result = server.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESTORE COMPLETE: no assignment is held for the restore.", result.stderr)

        calls = server.calls()
        # Nothing is stopped before the pre-flight has passed; the timer is paused before the service is stopped.
        self.assertEqual(calls[:4], ["py:check#1", "py:preflight#1", "timer-stop#1", "systemctl-stop#1"])
        self.assertEqual([call for call in calls if call.startswith("systemctl-")],
                         ["systemctl-stop#1", "systemctl-start#1"])
        self.assertEqual(calls[-3:], ["systemctl-start#1", "curl#1", "timer-start#1"])
        # hold, reconcile, clock, verify: recovery comes after reconciliation.
        self.assertEqual([call for call in calls if call.startswith("py:") and "fingerprint" not in call], [
            "py:check#1", "py:preflight#1", "py:--dry-run#1", "py:--apply#1", "py:registrations-restore-gate#1",
            "py:restore-hold#1", "py:restore-status#1", "py:reconcile#1", "py:clock-recover#1", "py:verify#1",
            "py:registrations-restore-status#1", "py:restore-status#2", "py:verify#2",
            "py:registrations-restore-status#2", "py:restore-status#3"])
        # Every gate on the copy, then uploads and vault, and the database last.
        moves = [call for call in calls if call.split("#")[0] in ("mv", "ln")]
        self.assertEqual(moves, ["mv#1", "mv#2", "mv#3", "mv#4", "ln#1", "mv#5"])
        self.assertLess(calls.index("py:restore-status#2"), calls.index("mv#1"))
        # What is examined is remembered, and compared just before and just after it is replaced:
        # the live database when it is stopped, the files when they are extracted, the copy when
        # it is verified; then the three again, the database set aside, and the one put in place.
        self.assertLess(calls.index("py:fingerprint#1"), calls.index("py:restore-hold#1"))
        self.assertLess(calls.index("py:fingerprint#3"), calls.index("py:verify#1"))
        self.assertEqual(calls[calls.index("py:restore-status#2") + 1:calls.index("install#4")],
                         ["py:fingerprint#4", "py:fingerprint#5", "py:fingerprint#6"])
        self.assertEqual(calls[calls.index("ln#1"):calls.index("py:verify#2")],
                         ["ln#1", "py:fingerprint#7", "mv#5", "py:fingerprint#8"])

        self.assert_restored(server)
        (replaced,) = server.replaced()
        self.assertEqual((replaced / "uploads/practical/live.txt").read_bytes(), server.original["uploads/practical/live.txt"])
        self.assertEqual((replaced / "questions.db").read_bytes(), server.original["data/questions.db"])
        # What happened after the snapshot is back: the closure and the extension.
        self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)
        self.assertEqual(server.assignment(self.extended)["deadline_at"], self.later)
        self.assertEqual(server.holds(), {self.closed: None, self.unchanged: None, self.extended: None})
        self.assertEqual(run_sql(server.live_db, "SELECT state FROM lifecycle_clock")[0][0], "ok")
        # The operator's name of three words reached the audit log as one argument.
        actions = [(entry["action"], entry["operator"]) for entry in server.audit()]
        self.assertEqual(actions, [("restore_hold", OPERATOR), ("reconcile", OPERATOR), ("clock_recover", OPERATOR)])
        reconciled = server.audit()[1]
        self.assertEqual(reconciled["evidence_note"], "грешен ъпдейт на друга таблица")
        self.assertEqual(reconciled["evidence_is_later_because"],
                         f"its lifecycle clock ran to {self.live_clock}; the snapshot's stood at {self.snapshot_clock}")
        self.assertEqual(reconciled["snapshot_db"], str(server.snapshot / "questions.db"))

    def test_restore_complete_does_not_say_that_every_assignment_is_writable(self):
        server = self.server()
        result = server.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        tail = result.stderr.split("RESTORE COMPLETE: no assignment is held for the restore.", 1)[1]
        self.assertIn("That is all it says. Closed assignments are closed and deadlines are what they were", tail)
        self.assertIn("can still be held for a clock review", tail)
        self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)  # complete, and closed

    def test_an_unresolved_restore_hold_is_never_a_successful_recovery(self):
        # The snapshot says closed, the database being replaced says open: the evidence contradicts a
        # terminal marker, so that assignment cannot be reconciled and stays held.
        snapshot = self.variant(self.snapshot_db, "snapshot", "UPDATE quiz_assignments SET closed_at = ? WHERE id = ?",
                                (self.closed_at, self.unchanged))
        server = self.server(snapshot_db=snapshot)

        result = server.run()
        self.assert_nothing_replaced(server, result, status=3)
        self.assertIn("RECOVERY INCOMPLETE: 1 assignment(s) are still held for the restore", result.stdout)
        self.assertIn("--accept-held", result.stderr)
        # An acknowledgement of another number is not an acknowledgement.
        for wrong in ("2", "3"):
            self.assert_nothing_replaced(server, server.run("--accept-held", wrong), status=3)

        result = server.run("--accept-held", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESTORED, NOT RECOVERED: 1 assignment(s) stay held and are closed to everyone.", result.stderr)
        self.assertNotIn("RESTORE COMPLETE", result.stderr)
        self.assertIn("ACKNOWLEDGED, NOT RECOVERED: 1 assignment(s) stay held", result.stdout)
        self.assert_restored(server)
        # Acknowledged is not released: the hold is exactly where reconcile left it.
        self.assertEqual(server.holds(), {self.closed: None, self.unchanged: "restore_pending,restore_unreconciled",
                                          self.extended: None})
        self.assertEqual(server.assignment(self.unchanged)["closed_at"], self.closed_at)  # the marker is kept

    def test_acknowledging_holds_that_do_not_exist_is_refused(self):
        server = self.server()
        result = server.run("--accept-held", "1")
        self.assert_nothing_replaced(server, result, status=1)
        self.assertIn("no assignment is held for the restore, but 1 were expected", result.stdout)

    def test_without_a_usable_replaced_database_everything_stays_held(self):
        server = self.server()
        result = server.run("--replaced-database-unreadable")
        self.assert_nothing_replaced(server, result, status=3)
        self.assertIn("RECOVERY INCOMPLETE: 3 assignment(s) are still held", result.stdout)

        result = server.run("--replaced-database-unreadable", "--accept-held", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse([call for call in server.calls() if call.startswith("py:reconcile")])  # nothing to reconcile from
        self.assertEqual(set(server.holds().values()), {"restore_pending"})
        self.assertIn("RESTORED, NOT RECOVERED: 3 assignment(s) stay held", result.stderr)
        self.assert_restored(server)
        # The stale snapshot state is in place, and held: the lost closure did not reopen anything.
        self.assertIsNone(server.assignment(self.closed)["closed_at"])
        # Set aside as it was, by a rename: the database that could not be used is kept whole.
        (replaced,) = server.replaced()
        self.assertEqual((replaced / "questions.db").read_bytes(), server.original["data/questions.db"])

    def test_the_time_is_asked_for_when_it_is_used_and_asked_again_when_it_is_refused(self):
        stale = utc(timedelta(minutes=-10)) + "\n"
        # Three readings that the server's clock does not agree with: the restore stops.
        server = self.server()
        result = server.run(stdin=stale * 3)
        self.assert_nothing_replaced(server, result, status=1)
        self.assertIn("gate failed: confirm the clock (clock-recover, exit status 1, 3 readings)", result.stderr)
        self.assertEqual(server.calls().count("py:clock-recover#3"), 1)
        self.assertNotIn("py:verify#1", server.calls())
        # One reading and no more: it stops with what clock-recover said, it does not go on.
        server = self.server()
        result = server.run(stdin=stale)
        self.assert_nothing_replaced(server, result, status=1)
        self.assertIn("no further reading was given", result.stderr)
        # Nothing given at all.
        server = self.server()
        result = server.run(stdin="")
        self.assert_nothing_replaced(server, result, status=2)
        self.assertIn("no verified time was given", result.stderr)
        # A mistyped reading costs one question, not the whole restore.
        server = self.server()
        result = server.run(stdin="yesterday at noon\n" + stale + utc() + "\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr.count("read now from a device other than this server"), 3)
        self.assertIn("py:clock-recover#3", server.calls())
        self.assertLess(server.calls().index("py:reconcile#1"), server.calls().index("py:clock-recover#1"))
        self.assert_restored(server)

    def test_a_time_or_a_finished_run_given_on_the_command_line_is_refused(self):
        server = self.server()
        result = server.run("--verified-utc", utc())
        self.assert_untouched(server, result, status=2)
        self.assertIn("a reading given in advance is stale by the time it is used", result.stderr)
        result = server.run("--complete-swap", "20260101T000000Z")
        self.assert_untouched(server, result, status=2)
        self.assertIn("--complete-swap does not exist any more", result.stderr)
        self.assertEqual(server.calls(), [])

    def test_a_declared_suspect_interval_reaches_clock_recover(self):
        server = self.server()
        start, end = utc(timedelta(hours=-3)), utc(timedelta(hours=-2))
        result = server.run("--suspect-from", start, "--suspect-to", end)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        recovered = [entry for entry in server.audit() if entry["action"] == "clock_recover"][0]
        self.assertEqual(recovered["suspect_interval"], [start, end])
        for half in (("--suspect-from", start), ("--suspect-to", end)):
            lonely = self.server()
            self.assert_untouched(lonely, lonely.run(*half), status=2)

    def test_a_deadline_the_replaced_clock_had_already_passed_does_not_come_back_open(self):
        # Claude review, M2. The replaced database's clock had run two hours ahead, and by it the
        # deadline set after the snapshot was over. The snapshot knows neither. clock-recover used to
        # run before reconcile, so the deadline arrived after the classification and the assignment
        # came back open and unheld.
        deadline = utc(timedelta(hours=1))
        live = self.variant(self.live_db, "live", "UPDATE quiz_assignments SET deadline_at = ? WHERE id = ?",
                            (deadline, self.unchanged))
        run_sql(live, "UPDATE lifecycle_clock SET high_water_at = ?", (utc(timedelta(hours=2)),))
        server = self.server(live_db=live)
        result = server.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = server.calls()
        self.assertLess(calls.index("py:reconcile#1"), calls.index("py:clock-recover#1"))
        row = server.assignment(self.unchanged)
        self.assertEqual((row["deadline_at"], row["lifecycle_hold"]), (deadline, "clock_review"))
        self.assertIsNone(row["expiry_observed_at"])              # nothing was manufactured
        self.assertIn("held for another reason only: 1", result.stdout)
        self.assertEqual(server.holds()[self.closed], None)

    def test_what_the_replaced_database_held_only_in_its_write_ahead_log_is_not_lost(self):
        # The service died with a closure committed to the write-ahead log and not yet to the main file.
        live = self.variant(self.snapshot_db, "live")
        conn = sqlite3.connect(str(live))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("UPDATE quiz_assignments SET closed_at = ? WHERE id = ?", (self.closed_at, self.closed))
            conn.execute("UPDATE lifecycle_clock SET high_water_at = ?", (self.live_clock,))   # as a close does
            conn.commit()
            self.assertGreater(Path(f"{live}-wal").stat().st_size, 0)
            server = self.server(live_db=live)      # the main file, as it is on the disk now
            for suffix in ("-wal", "-shm"):
                shutil.copyfile(f"{live}{suffix}", f"{server.live_db}{suffix}")
        finally:
            conn.close()
        self.assertIsNone(run_sql(self.variant(server.live_db, "main-file-alone"),
                                  "SELECT closed_at FROM quiz_assignments WHERE id = ?", (self.closed,))[0][0])

        result = server.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)   # reconciled from the log
        (replaced,) = server.replaced()
        kept = self.variant(replaced / "questions.db", "kept")                           # and kept, in one file
        self.assertEqual(run_sql(kept, "SELECT closed_at FROM quiz_assignments WHERE id = ?", (self.closed,))[0][0],
                         self.closed_at)
        self.assertFalse(Path(f"{server.live_db}-wal").exists() and Path(f"{server.live_db}-wal").stat().st_size
                         and not server.active())

    def test_a_run_that_stopped_before_the_swap_can_simply_be_repeated(self):
        server = self.server()
        result = server.run(fail="py:reconcile#1=2")
        self.assert_nothing_replaced(server, result, status=2)
        # The repeat reconciles from the database that was really live: nothing was mistaken for it.
        result = server.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_restored(server)
        self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)
        self.assertEqual(server.holds(), {self.closed: None, self.unchanged: None, self.extended: None})

    def test_a_snapshot_from_before_the_lifecycle_migration_is_migrated_before_it_is_held(self):
        # First Codex review, finding 7: restore-hold cannot run on a schema without migration 018.
        old = self.fixtures / "before-018.db"
        sqlite3.connect(old).close()
        previous = self.fixtures / "migrations-before-018"
        previous.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < "018_":
                shutil.copyfile(path, previous / path.name)
        with open(os.devnull, "w", encoding="utf-8") as sink:
            self.assertEqual(manage_migrations.apply_migrations(old, previous, out=sink), 0)
        with closing(sqlite3.connect(str(old))) as conn:
            conn.executescript(POPULATE_SQL)
            conn.commit()
        self.assertNotIn("lifecycle_hold", {row[1] for row in run_sql(old, "PRAGMA table_info(quiz_assignments)")})
        # The live database is the same data after this release was deployed, and then used:
        # migrated, and one assignment closed.
        live = self.fixtures / "after-018.db"
        shutil.copyfile(old, live)
        with open(os.devnull, "w", encoding="utf-8") as sink:
            self.assertEqual(manage_migrations.apply_migrations(live, MIGRATIONS, out=sink), 0)
        first = run_sql(live, "SELECT MIN(id) AS id FROM quiz_assignments")[0]["id"]
        run_sql(live, "UPDATE quiz_assignments SET closed_at = ? WHERE id = ?", (self.closed_at, first))
        run_sql(live, "UPDATE lifecycle_clock SET high_water_at = ?", (self.live_clock,))

        server = self.server(live_db=live, snapshot_db=old)
        result = server.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = server.calls()
        self.assertLess(calls.index("py:--apply#1"), calls.index("py:restore-hold#1"))
        self.assertIn("018_assignment_lifecycle.sql", result.stdout)
        self.assertIn("lifecycle_hold", {row[1] for row in run_sql(server.live_db, "PRAGMA table_info(quiz_assignments)")})
        self.assertEqual(server.assignment(first)["closed_at"], self.closed_at)
        self.assertEqual(set(server.holds().values()), {None})

        # Claude review, N3. The same release installed and never used: its lifecycle clock stands
        # where the migration put it. A copy of the snapshot, migrated, looks exactly like that, so
        # it is not evidence of anything that came after the snapshot, whatever else is in it.
        never_used = self.fixtures / "after-018-never-used.db"
        shutil.copyfile(old, never_used)
        with open(os.devnull, "w", encoding="utf-8") as sink:
            self.assertEqual(manage_migrations.apply_migrations(never_used, MIGRATIONS, out=sink), 0)
        self.assertEqual(run_sql(never_used, "SELECT high_water_at FROM lifecycle_clock")[0][0], lifecycle.CLOCK_SEED)
        run_sql(never_used, "UPDATE quiz_assignments SET title_bg = 'Преименуван' WHERE id = ?", (first,))
        server = self.server(live_db=never_used, snapshot_db=old)
        result = server.run()
        self.assert_nothing_replaced(server, result, status=2)
        self.assertIn("it shows no lifecycle activity after the snapshot", result.stderr)
        self.assertIn("had not started: the snapshot is from before the lifecycle existed", result.stderr)
        self.assertNotIn("py:clock-recover#1", server.calls())

    def test_wrong_arguments_touch_nothing_and_do_not_stop_the_service(self):
        server = self.server()
        cases = {
            "no operator": dict(operator=" "),
            "no evidence note": dict(note=" "),
            "note and unreadable together": dict(arguments=("--replaced-database-unreadable", "--evidence-note", "x")),
            "accept-held is not a number": dict(arguments=("--accept-held", "all")),
            "unknown option": dict(arguments=("--start-anyway",)),
            "a value is missing": dict(arguments=("--accept-held",)),
        }
        for label, case in cases.items():
            with self.subTest(case=label):
                arguments = case.pop("arguments", ())
                self.assert_untouched(server, server.run(*arguments, **case), status=2)
        self.assertEqual(server.calls(), [])

    def test_a_service_that_does_not_stop_stops_the_restore(self):
        server = self.server()
        result = server.run(RESTORE_TEST_STOP_DOES_NOTHING="1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("the service is still active after it was stopped", result.stderr)
        self.assertIn(NOT_REPLACED, result.stderr)
        self.assertFalse(server.started())
        self.assertEqual((server.live_files(), server.staging(), server.replaced()), (server.original, [], []))
        self.assertFalse(server.locked())
        self.assertNotIn("py:restore-hold#1", server.calls())


class ExaminedIsReplacedTest(RestoreScriptTestCase):
    """Within one run: what is set aside is what was examined, and what is put in place is what
    was verified, to the byte. (Between the two a person is asked for the time; that can take a while.)"""

    def meanwhile(self, server: Restore, name: str, python: str) -> str:
        """An executable that does something behind the script's back; run by a shim at a chosen call."""
        path = server.root / name
        path.write_text(f"#!/bin/bash\n{sys.executable} -B - <<'PY'\nimport glob, os, sqlite3\n"
                        f"state = os.environ['LEARNPILOT_STATE']\n{python}\nPY\n", encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def test_a_live_database_that_changed_after_it_was_examined_is_not_replaced(self):
        # The reproduction of the review, inside one run: after reconciliation, an assignment is
        # closed in the live database. Replacing it now would reopen that assignment.
        server = self.server()
        closed_meanwhile = utc(timedelta(minutes=-1))
        hook = self.meanwhile(server, "close-one", f"""
conn = sqlite3.connect(os.path.join(state, 'data', 'questions.db'))
conn.execute("UPDATE quiz_assignments SET closed_at = ? WHERE id = ?", ({closed_meanwhile!r}, {self.unchanged}))
conn.commit()""")
        result = server.run(RESTORE_TEST_HOOK=f"py:clock-recover#1={hook}")
        self.assert_not_started(server, result, status=1)
        self.assertIn("gate failed: the database being replaced is byte for byte the one that was examined",
                      result.stderr)
        self.assertIn("FAIL: changed since it was examined", result.stdout)
        self.assertIn(NOT_REPLACED, result.stderr)
        self.assertEqual((server.staging(), server.replaced(), server.locked()), ([], [], False))
        self.assertEqual(server.assignment(self.unchanged)["closed_at"], closed_meanwhile)   # not overwritten
        self.assertNotIn("mv#1", server.calls())
        # Run again, it is reconciled against the live database as it now is: closed stays closed.
        server.original = server.live_files()
        again = server.run()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assert_restored(server)
        self.assertEqual(server.assignment(self.unchanged)["closed_at"], closed_meanwhile)

    def test_a_live_database_that_changes_in_the_last_moment_is_caught_under_its_second_name(self):
        # After the last look at it and before the rename that replaces it: the file is read once
        # more, as the copy that is being kept, and the rename does not happen.
        server = self.server()
        hook = self.meanwhile(server, "close-late", f"""
conn = sqlite3.connect(os.path.join(state, 'data', 'questions.db'))
conn.execute("UPDATE quiz_assignments SET title_bg = 'Променен в последния миг' WHERE id = ?", ({self.unchanged},))
conn.commit()""")
        result = server.run(RESTORE_TEST_HOOK=f"ln#1={hook}")
        self.assert_not_started(server, result, status=1)
        self.assertIn("gate failed: the database being set aside is byte for byte the one that was examined",
                      result.stderr)
        self.assertIn("taken back completely", result.stderr)
        after = server.calls()[server.calls().index("py:fingerprint#7") + 1:]
        # What is on the disk is looked at first (the live database is not the copy; the copy is still
        # where it was staged), and then the four moves are taken back.
        self.assertEqual(after, ["py:fingerprint#8", "py:fingerprint#9",
                                 "mv#5", "mv#6", "mv#7", "mv#8", "timer-start#1"])
        self.assertEqual(server.assignment(self.unchanged)["title_bg"], "Променен в последния миг")   # not replaced
        self.assertEqual((server.staging(), server.replaced(), server.locked()), ([], [], False))
        self.assertTrue((server.state / "uploads/practical/live.txt").exists())   # and the files are back

    def test_a_staged_database_that_changed_after_it_was_verified_is_not_put_in_place(self):
        server = self.server()
        # The copy passed `verify`; then every hold and every closure in it is wiped.
        hook = self.meanwhile(server, "wipe-the-copy", """
(copy,) = glob.glob(os.path.join(state, 'restore-staging-*', 'questions.db'))
conn = sqlite3.connect(copy)
conn.execute("UPDATE quiz_assignments SET closed_at = NULL, lifecycle_hold = NULL")
conn.commit()
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")""")
        result = server.run(RESTORE_TEST_HOOK=f"py:restore-status#2={hook}")
        self.assert_nothing_replaced(server, result, status=1)
        self.assertIn("gate failed: the staged database is byte for byte the one that was verified", result.stderr)
        self.assertNotIn("mv#1", server.calls())

    def test_staged_files_that_changed_after_they_were_extracted_are_not_put_in_place(self):
        server = self.server()
        hook = self.meanwhile(server, "change-a-file", """
(note,) = glob.glob(os.path.join(state, 'restore-staging-*', 'vault', 'Generated', 'restored.md'))
with open(note, 'a', encoding='utf-8') as handle:
    handle.write('changed after it was extracted')""")
        result = server.run(RESTORE_TEST_HOOK=f"py:verify#1={hook}")
        self.assert_nothing_replaced(server, result, status=1)
        self.assertIn("gate failed: the staged uploads and vault are byte for byte what was extracted", result.stderr)
        # A file added to the copy is a change as well.
        server = self.server()
        hook = self.meanwhile(server, "add-a-file", """
(uploads,) = glob.glob(os.path.join(state, 'restore-staging-*', 'uploads', 'practical'))
open(os.path.join(uploads, 'slipped-in.txt'), 'w').close()""")
        result = server.run(RESTORE_TEST_HOOK=f"py:verify#1={hook}")
        self.assert_nothing_replaced(server, result, status=1)

    def test_the_digest_is_of_every_byte_and_is_kept_nowhere(self):
        directory = tempfile.TemporaryDirectory(prefix="lifecycle-fingerprint-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "tree/sub").mkdir(parents=True)
        (root / "tree/sub/a.txt").write_text("a", encoding="utf-8")
        (root / "file.db").write_bytes(b"database" * 1000)
        both = [root / "file.db", root / "tree"]
        first = restore_preflight.fingerprint(both)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(restore_preflight.fingerprint(both), first)
        changes = {
            "a byte of the file": lambda: (root / "file.db").write_bytes(b"databasE" + b"database" * 999),
            "a byte in the tree": lambda: (root / "tree/sub/a.txt").write_text("b", encoding="utf-8"),
            "a file renamed": lambda: (root / "tree/sub/a.txt").rename(root / "tree/sub/b.txt"),
            "an empty file added": lambda: (root / "tree/empty").touch(),
            "an empty directory added": lambda: (root / "tree/another").mkdir(),
        }
        seen = {first}
        for label, change in changes.items():
            with self.subTest(change=label):
                change()
                now = restore_preflight.fingerprint(both)
                self.assertNotIn(now, seen)
                seen.add(now)
        out = io.StringIO()
        self.assertEqual(restore_preflight.main(["--fingerprint", *map(str, both)], out=out), 0)
        latest = out.getvalue().strip()
        self.assertEqual(restore_preflight.main(["--fingerprint", *map(str, both), "--expect", latest], out=out), 0)
        self.assertEqual(restore_preflight.main(["--fingerprint", *map(str, both), "--expect", first], out=out), 1)
        self.assertIn("FAIL: changed since it was examined", out.getvalue())
        self.assertEqual(restore_preflight.main(["--fingerprint", str(root / "gone")], out=out), 1)
        self.assertEqual({path.name for path in root.iterdir()}, {"file.db", "tree"})    # it wrote nothing
        # In the script the digests live in shell variables: never in a file, never in the lock.
        script = SCRIPT.read_text(encoding="utf-8")
        for variable in ("LIVE_SEEN", "FILES_STAGED", "VERIFIED"):
            self.assertRegex(script, rf'(?m)^\s*{variable}=\$\("\$\{{FINGERPRINT\[@\]\}}"')
            for use in re.findall(rf".*\${variable}\b.*", script):
                self.assertRegex(use, rf'--expect "\${variable}"( > /dev/null; then)?$', use)


class StoppedRightAfterAStepTest(RestoreScriptTestCase):
    """Claude review, N1. The script is stopped right after a rename has really happened and before
    it has noted anything: by a signal, or by the step reporting failure although it did its work.
    Taking that back by what the script believed deleted the only name the database that was live
    still had, and released the lock with the restored database in place."""

    HOW = {"TERM": 143, "INT": 130, "HUP": 129, "130": 130}

    def assert_replaced_and_locked(self, server: Restore, result):
        self.assertFalse(server.started())
        for never_said in ("Nothing was replaced", "taken back", "The restore lock was released"):
            self.assertNotIn(never_said, result.stderr)
        self.assertIn("The database HAD BEEN REPLACED when the script was stopped", result.stderr)
        self.assertIn("The lock stays. Run the script again from the beginning.", result.stderr)
        self.assertIn(NOT_STARTED, result.stderr)
        self.assertEqual((server.locked(), server.active(), server.timer_active()), (True, False, False))
        self.assertIn("in progress or was left unfinished", server.application_refuses())
        with self.assertRaises(BackupError):
            server.backup()
        # In place: the copy, with the snapshot's files. Nothing was moved back around it.
        self.assertNotEqual(server.live_db.read_bytes(), server.original["data/questions.db"])
        self.assertTrue((server.state / "uploads/practical/restored.txt").exists())
        self.assertTrue((server.state / "vault/Generated/restored.md").exists())
        self.assertEqual(sorted(path.name for path in server.live_db.parent.glob("questions.db*")), ["questions.db"])
        # And the database that was live is still on the disk, whole, in the one place it has left.
        (replaced,) = server.replaced()
        self.assertEqual((replaced / "questions.db").read_bytes(), server.original["data/questions.db"])
        kept = server.everything()
        for relative, content in server.original.items():
            self.assertIn(content, kept, relative)

    def test_stopped_right_after_the_database_was_replaced_nothing_is_taken_back_or_unlocked(self):
        def stopped(how):
            server = self.server()
            result = server.run(after=f"mv#5={how}")       # the rename that replaces the database
            self.assertEqual(result.returncode, self.HOW[how], result.stderr)
            self.assertEqual([call for call in server.calls() if call.startswith("mv#")][-1], "mv#5")   # no move after it
            self.assert_replaced_and_locked(server, result)
            self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)   # the copy, as reconciled
            # Run again, it is a restore over the state that is now in place, with every gate.
            again = server.run()
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assert_restored(server, abandoned=1)      # the stopped run's emptied staging directory stays
            self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)
            self.assertEqual(server.holds(), {self.closed: None, self.unchanged: None, self.extended: None})

        self.each(self.HOW, stopped)

    def unusable(self) -> Restore:
        """A server whose live database has a write-ahead log and index beside it, with content."""
        server = self.server()
        Path(f"{server.live_db}-wal").write_bytes(b"the write-ahead log of the database that was live\n" * 40)
        Path(f"{server.live_db}-shm").write_bytes(b"and its index\n" * 40)
        return server

    def test_the_same_when_the_database_being_replaced_was_not_usable(self):
        arguments = ("--replaced-database-unreadable", "--accept-held", "3")
        control = self.unusable()
        self.assertEqual(control.run(*arguments).returncode, 0)
        moves = [call for call in control.calls() if call.startswith("mv#")]
        self.assertEqual(len(moves), 8)        # uploads and vault (4), the database and its two sidecars, the copy
        last = moves[-1]

        def stopped(how):
            server = self.unusable()
            sidecars = {suffix: Path(f"{server.live_db}{suffix}").read_bytes() for suffix in ("-wal", "-shm")}
            result = server.run(*arguments, after=f"{last}={how}")
            self.assertEqual(result.returncode, self.HOW[how], result.stderr)
            self.assert_replaced_and_locked(server, result)     # no old write-ahead log beside the new database
            (replaced,) = server.replaced()
            for suffix, content in sidecars.items():
                self.assertEqual((replaced / f"questions.db{suffix}").read_bytes(), content)
            again = server.run(*arguments)
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertEqual(set(server.holds().values()), {"restore_pending"})

        self.each(self.HOW, stopped)

        # Stopped earlier, when only a part of the old database had been set aside: all of it comes back.
        def stopped_before(call):
            server = self.unusable()
            sidecars = {suffix: Path(f"{server.live_db}{suffix}").read_bytes() for suffix in ("-wal", "-shm")}
            result = server.run(*arguments, after=f"{call}=TERM")
            self.assertEqual(result.returncode, 143, result.stderr)
            self.assert_nothing_replaced(server, result)
            self.assertIn("taken back completely", result.stderr)
            for suffix, content in sidecars.items():
                self.assertEqual(Path(f"{server.live_db}{suffix}").read_bytes(), content)

        self.each(moves[4:7], stopped_before)

    def test_an_unusable_database_that_cannot_be_put_back_whole_is_not_said_to_be_in_place(self):
        # Its write-ahead log cannot be set aside (the sixth move), and the main file, already set
        # aside, cannot be moved back either (the seventh, the first step of taking it back).
        server = self.unusable()
        result = server.run("--replaced-database-unreadable", "--accept-held", "3", fail="mv#6=1 mv#7=1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Could not be taken back", result.stderr)
        self.assertIn("The copy was NOT put in place as the live database.", result.stderr)
        for never_said in ("it is still the one that was live", "Nothing was replaced", "taken back completely",
                           "HAD BEEN REPLACED", "The restore lock was released"):
            self.assertNotIn(never_said, result.stderr)
        self.assertEqual((server.locked(), server.active(), server.started()), (True, False, False))
        self.assertFalse(server.live_db.exists())                 # no database to start on, and the lock says so
        (replaced,) = server.replaced()
        self.assertEqual((replaced / "questions.db").read_bytes(), server.original["data/questions.db"])
        self.assertTrue(Path(f"{server.live_db}-wal").exists())   # never moved: not beside any other database

    def test_stopped_right_after_any_earlier_step_everything_is_taken_back(self):
        # The same race one step earlier: the rename happened, the script had not noted it yet.
        def stopped(case):
            call, how = case
            server = self.server()
            result = server.run(after=f"{call}={how}")
            self.assertEqual(result.returncode, self.HOW[how], result.stderr)
            self.assert_nothing_replaced(server, result)
            self.assertIn("taken back completely", result.stderr)
            self.assertEqual(server.live_db.stat().st_nlink, 1)       # and no second name is left of it

        self.each([(call, how) for call in ("mv#1", "mv#2", "mv#3", "mv#4", "ln#1") for how in ("TERM", "130")],
                  stopped)

    def test_when_it_cannot_be_told_whether_the_database_was_replaced_nothing_is_moved_and_the_lock_stays(self):
        # Stopped right after the rename, and by the time the script looks, the database at the live
        # path is no longer, byte for byte, the copy it verified.
        server = self.server()
        hook = server.root / "something-writes"
        hook.write_text('#!/bin/bash\nprintf x >> "$LEARNPILOT_STATE/data/questions.db"\n', encoding="utf-8")
        hook.chmod(0o700)
        result = server.run(after="mv#5=TERM", RESTORE_TEST_HOOK=f"py:fingerprint#8={hook}")
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertIn("WHETHER THE DATABASE WAS REPLACED COULD NOT BE ESTABLISHED", result.stderr)
        self.assertIn("Nothing was moved back.", result.stderr)
        self.assertIn("INTERRUPTED WHILE REPLACING the live state.", result.stderr)
        for never_said in ("Nothing was replaced", "taken back completely", "The restore lock was released",
                           "was not replaced"):
            self.assertNotIn(never_said, result.stderr)
        self.assertEqual((server.locked(), server.active(), server.started()), (True, False, False))
        (replaced,) = server.replaced()
        self.assertEqual((replaced / "questions.db").read_bytes(), server.original["data/questions.db"])
        self.assertTrue((server.state / "uploads/practical/restored.txt").exists())   # nothing was moved back

    def test_stopped_right_after_the_start_the_service_is_stopped_again(self):
        server = self.server()
        result = server.run(after="systemctl-start#1=TERM")
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertIn("The service was stopped again.", result.stderr)
        self.assertIn("The restore itself is complete and the lock is released; the service is not running.",
                      result.stderr)
        self.assertIn(NOT_STARTED, result.stderr)
        self.assertEqual((server.active(), server.locked(), server.timer_active()), (False, False, True))
        self.assertEqual(server.calls()[-3:], ["systemctl-start#1", "systemctl-stop#2", "timer-start#1"])


class ServiceSettingsTest(RestoreScriptTestCase):
    """Claude review, N4. The environment file was searched for one matching line. A second
    DZI_DB further down, which is the one systemd uses, went unnoticed: the pre-flight checked one
    database and the service ran on another."""

    def test_a_setting_that_is_ambiguous_or_points_elsewhere_stops_the_restore_before_it_begins(self):
        def rewrite(server, name, line):
            lines = server.env_file.read_text(encoding="utf-8").splitlines()
            kept = [text for text in lines if not text.startswith(name + "=")]
            server.env_file.write_text("\n".join([*kept, *([line] if line else [])]) + "\n", encoding="utf-8")

        def append(server, line):
            with open(server.env_file, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

        def unit(server, name, text):
            (server.run_dir / name).write_text(text, encoding="utf-8")

        cases = {
            "a second DZI_DB further down, the one systemd would use": (
                lambda server: append(server, "DZI_DB=/var/lib/another/questions.db"), "DZI_DB is set 2 times"),
            "the same line twice": (lambda server: append(server, f"DZI_DB={server.live_db}"), "DZI_DB is set 2 times"),
            "a second one, exported": (
                lambda server: append(server, "export DZI_DB=/var/lib/another/questions.db"), "DZI_DB is set 2 times"),
            "a second one, indented and spaced": (
                lambda server: append(server, "  DZI_DB = /var/lib/another/questions.db"), "DZI_DB is set 2 times"),
            "not set at all": (lambda server: rewrite(server, "DZI_DB", ""), "DZI_DB is set 0 times"),
            "quoted": (lambda server: rewrite(server, "DZI_DB", f'DZI_DB="{server.live_db}"'),
                       "written as plain DZI_DB=PATH"),
            "another database": (lambda server: rewrite(server, "DZI_DB", "DZI_DB=/var/lib/another/questions.db"),
                                 "DZI_DB in"),
            "the uploads somewhere else": (
                lambda server: rewrite(server, "DZI_UPLOAD_ROOT", "DZI_UPLOAD_ROOT=/mnt/volume/practical"),
                "DZI_UPLOAD_ROOT in"),
            "two upload roots": (lambda server: append(server, "DZI_UPLOAD_ROOT=/mnt/volume/practical"),
                                 "DZI_UPLOAD_ROOT is set 2 times"),
            "the vault somewhere else": (lambda server: rewrite(server, "DZI_VAULT", "DZI_VAULT=/srv/vault"),
                                         "DZI_VAULT in"),
            "a state path in the unit itself as well": (
                lambda server: unit(server, "unit-environment",
                                    "Environment=PYTHONUNBUFFERED=1 DZI_DB=/var/lib/another/questions.db\n"),
                "a state path is set in the unit itself (Environment=) as well as in"),
            "two environment files": (
                lambda server: unit(server, "unit-environment-files",
                                    f"EnvironmentFiles={server.env_file} (ignore_errors=no)\n"
                                    "EnvironmentFiles=/etc/learnpilot/override.env (ignore_errors=no)\n"),
                "the service reads more than one environment file"),
            "an environment file that may be missing": (
                lambda server: unit(server, "unit-environment-files",
                                    f"EnvironmentFiles={server.env_file} (ignore_errors=yes)\n"),
                "the service has no single, mandatory environment file"),
            "no environment file": (lambda server: unit(server, "unit-environment-files", "EnvironmentFiles=\n"),
                                    "the service has no single, mandatory environment file"),
            "systemd cannot be asked": (lambda server: (server.run_dir / "unit-environment-files").unlink(),
                                        "the settings of the service could not be read from systemd"),
            "the environment file is gone": (lambda server: server.env_file.unlink(),
                                             "gate failed: the service is set, once and plainly"),
        }

        def refused(label):
            change, message = cases[label]
            server = self.server()
            change(server)
            result = server.run()
            self.assertIn(message, result.stderr)
            self.assert_untouched(server, result)
            self.assertNotIn("another/questions.db", result.stdout + result.stderr)   # nor any other value of it

        self.each(cases, refused)

    def test_comments_and_other_settings_do_not_confuse_it_and_no_value_is_ever_shown(self):
        server = self.server()
        with open(server.env_file, "a", encoding="utf-8") as handle:
            handle.write("# DZI_DB=/var/lib/learnpilot-old/questions.db   (before the move)\n"
                         "DZI_DB_POOL=4\nMY_DZI_DB=/elsewhere\nDZI_REFERENCE_ROOT=/var/lib/learnpilot/data/reference\n")
        result = server.run()                      # (every run of this module checks that the secret is not printed)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("learnpilot-old", result.stdout + result.stderr)
        self.assertIn(SECRET, server.env_file.read_text(encoding="utf-8"))


class PreflightFailureTest(RestoreScriptTestCase):
    """Whatever the pre-flight finds, it finds before anything is stopped, locked or modified."""

    def test_each_failed_preflight_check_leaves_everything_as_it_was(self):
        def without(name):
            return lambda server: (server.snapshot / name).unlink()

        def archive_with_an_entry_outside(server):
            with tarfile.open(server.snapshot / "files.tar.gz", "w:gz") as archive:
                info = tarfile.TarInfo("data/questions.db")
                archive.addfile(info, io.BytesIO(b""))

        checks = {
            "the snapshot database is missing": (without("questions.db"), {}, "the snapshot has no questions.db"),
            "the snapshot database is damaged": (
                lambda server: (server.snapshot / "questions.db").write_bytes(b"damaged" * 4000), {},
                "the snapshot database"),
            "the archive is missing": (without("files.tar.gz"), {}, "the snapshot has no files.tar.gz"),
            "the archive is cut short": (
                lambda server: (server.snapshot / "files.tar.gz").write_bytes(
                    (server.snapshot / "files.tar.gz").read_bytes()[:60]), {}, "cannot be read to its end"),
            "the archive reaches outside": (archive_with_an_entry_outside, {}, "an entry outside uploads/ and vault/"),
            "there is no database to replace": (
                lambda server: server.live_db.rename(server.root / "moved-away.db"), {},
                "the database to be replaced exists"),
            "the audit log cannot be written": (
                lambda server: None, {"LEARNPILOT_AUDIT_LOG": "/nonexistent-directory/operator.log"},
                "the audit log can be written by the service user"),
            "the application's Python is not there": (
                lambda server: None, {"LEARNPILOT_PYTHON": "/nonexistent/python"},
                "the application's Python can be run as the service user"),
            "the checkout is somewhere else": (
                lambda server: None, {"LEARNPILOT_APP": "/nonexistent/app"},
                "the application checkout is the working directory"),
            "a backup is running": (
                lambda server: (server.run_dir / "backup-state").write_text("activating", encoding="utf-8"), {},
                "a backup is running"),
            "the uploads are behind a symbolic link": (
                lambda server: ((server.state / "uploads/practical").rename(server.root / "uploads-on-a-volume"),
                                (server.state / "uploads/practical").symlink_to(server.root / "uploads-on-a-volume")),
                {}, "is a symbolic link: the restore replaces uploads as a whole"),
            "the state directory itself is a symbolic link": (
                lambda server: (server.state.rename(server.root / "state-on-a-volume"),
                                server.state.symlink_to(server.root / "state-on-a-volume")),
                {}, f"is a symbolic link (to "),
        }
        def fails(label):
            damage, environment, message = checks[label]
            server = self.server()
            damage(server)
            server.original = server.live_files()
            result = server.run(**environment)
            self.assertIn(message, result.stdout + result.stderr)
            self.assert_untouched(server, result)

        self.each(checks, fails)

    def test_a_missing_program_is_found_before_anything_is_stopped(self):
        server = self.server()
        programs = self.fixtures / "programs"
        programs.mkdir()
        for name in ("rm", "mkdir", "cat", "grep", "ps", "date", "sleep", "bash"):   # everything it needs, but rmdir
            (programs / name).symlink_to(shutil.which(name))
        result = server.run(PATH=f"{server.shims}{os.pathsep}{programs}")
        self.assert_untouched(server, result, status=2)
        self.assertIn("a program this script needs is not installed: rmdir", result.stderr)
        self.assertEqual(server.calls(), [])


class PredecessorProvenanceTest(RestoreScriptTestCase):
    """Claude review, M1, through the script: what is at the live path has to be the later state."""

    def test_a_copy_of_the_snapshot_at_the_live_path_releases_nothing(self):
        # Somebody put the snapshot in place by hand first. It says "open" about an assignment that
        # the database it overwrote had closed.
        server = self.server(live_db=self.snapshot_db)
        result = server.run()
        self.assert_nothing_replaced(server, result, status=2)
        self.assertIn("gate failed: reconcile from the database being replaced (exit status 2)", result.stderr)
        self.assertIn("it shows no lifecycle activity after the snapshot", result.stderr)
        self.assertIn("Nothing was reconciled and every assignment stays held", result.stderr)
        self.assertNotIn("py:clock-recover#1", server.calls())

    def test_a_copy_of_the_snapshot_with_something_else_changed_in_it_releases_nothing_either(self):
        # Claude review, N3: an added account made such a copy "differ", and that was taken for life
        # after the snapshot. A new users row is not lifecycle history.
        live = self.variant(self.snapshot_db, "live",
                            "INSERT INTO users (display_name, role, session_key, created_at, updated_at) "
                            "VALUES ('Нов учител', 'teacher', ?, ?, ?)", ("k" * 32, utc(), utc()))
        run_sql(live, "UPDATE quiz_assignments SET title_bg = 'Преименуван след копието'")
        server = self.server(live_db=live)
        result = server.run()
        self.assert_nothing_replaced(server, result, status=2)
        self.assertIn("it shows no lifecycle activity after the snapshot", result.stderr)
        self.assertIn("an account, a sign-in or an edited title says nothing about deadlines and closures",
                      result.stderr)
        self.assertNotIn("py:clock-recover#1", server.calls())

    def test_another_database_with_the_same_assignment_ids_releases_nothing(self):
        other = build_test_database(self.fixtures / "another-school.db")
        for number in range(3):
            run_sql(other, "INSERT INTO quiz_assignments (section_id, title_bg, question_count, created_at, "
                           "public_token) VALUES (?, 'Чужд', 1, ?, ?)",
                    (self.section, utc(timedelta(days=-3)), f"another-token-{number}"))
        self.assertEqual([row["id"] for row in run_sql(other, "SELECT id FROM quiz_assignments ORDER BY id")],
                         [self.closed, self.unchanged, self.extended])
        server = self.server(live_db=other)
        result = server.run()
        self.assert_nothing_replaced(server, result, status=2)
        self.assertIn("it is not a later state of this database", result.stderr)
        self.assertIn("The same ids are not the same rows", result.stderr)

    def test_the_database_replaced_is_the_one_the_service_is_configured_for(self):
        server = self.server()
        elsewhere = server.root / "elsewhere"
        shutil.copytree(server.state, elsewhere)
        # LEARNPILOT_STATE pointed at a directory the service does not use: refused in the pre-flight.
        result = server.run(LEARNPILOT_STATE=str(elsewhere))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"DZI_DB in {server.env_file} is not {elsewhere}/data/questions.db", result.stderr)
        self.assertIn(UNTOUCHED, result.stderr)
        self.assertEqual((server.active(), server.locked(), server.live_files()), (True, False, server.original))


class RestoreScriptGateTest(RestoreScriptTestCase):
    """Failure injected at every step: the start is never reached."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        control = Restore(cls.addClassCleanup, cls.live_db, cls.snapshot_db)
        result = control.run()
        assert result.returncode == 0, result.stdout + result.stderr
        cls.control_calls = control.calls()

    def test_the_control_run_passes_every_gate_once(self):
        calls = self.control_calls
        self.assertEqual(calls.count("systemctl-start#1"), 1)
        self.assertEqual(calls[-3:], ["systemctl-start#1", "curl#1", "timer-start#1"])
        names = {call.split("#")[0] for call in calls}
        self.assertEqual(names, {"systemctl-stop", "systemctl-start", "timer-stop", "timer-start", "curl", "sqlite3",
                                 "tar", "install", "mv", "ln", "py:check", "py:preflight", "py:fingerprint",
                                 "py:--dry-run",
                                 "py:--apply", "py:restore-hold", "py:restore-status", "py:clock-recover",
                                 "py:registrations-restore-gate", "py:registrations-restore-status",
                                 "py:reconcile", "py:verify"})
        self.assertGreaterEqual(calls.index("systemctl-start#1"), 35)

    def test_a_failure_at_any_step_before_the_start_never_leads_to_a_start(self):
        calls = self.control_calls
        before_start = calls[:calls.index("systemctl-start#1")]
        stop = before_start.index("systemctl-stop#1")
        first_move, replaced = before_start.index("mv#1"), before_start.index("mv#5")

        def fail_at(position):
            call = before_start[position]
            server = self.server()
            result = server.run(fail=f"{call}=7")
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertFalse(server.started(), server.calls())
            self.assertEqual(server.calls()[:position + 1], before_start[:position + 1])   # it got exactly there
            self.assertNotIn("RESTORE COMPLETE", result.stderr)
            if call in ("py:check#1", "py:preflight#1"):
                self.assert_untouched(server, result)
            elif position <= stop:
                # The lock was taken and given back; the service had not been stopped yet.
                self.assertIn(NOT_REPLACED, result.stderr)
                self.assertEqual((server.active(), server.locked(), server.timer_active()), (True, False, True))
                self.assertEqual(server.live_files(), server.original)
            elif position < replaced:
                self.assert_nothing_replaced(server, result)
                self.assertEqual("taken back completely" in result.stderr, position >= first_move)
            elif position == replaced:
                # The rename that replaces the database did not happen: everything is taken back.
                self.assert_nothing_replaced(server, result)
                self.assertIn("taken back completely", result.stderr)
            else:
                # The database was replaced: complete and gated, and still locked.
                self.assertIn("The restored state is in place", result.stderr)
                self.assertIn("The lock stays. Run the script again from the beginning.", result.stderr)
                self.assertEqual((server.active(), server.locked(), server.timer_active()), (False, True, False))
                self.assertEqual(server.staging(), [])
                self.assertIsNotNone(server.application_refuses())

        self.each(range(len(before_start)), fail_at)
        self.assertGreater(len(before_start), 35)

    def test_every_lifecycle_gate_stops_it_whatever_the_status(self):
        # Every call was already failed with status 7 above. Here: 3, which only reconcile may
        # return without being a failure, and 1, which is also what a crash returns.
        gates = [call for call in self.control_calls if call.startswith("py:")
                 and call not in ("py:check#1", "py:preflight#1") and "fingerprint" not in call]
        self.assertEqual(len(gates), 13)   # the three lifecycle-independent registration calls included

        def fail_with(case):
            call, status = case
            server = self.server()
            result = server.run(fail=f"{call}={status}")
            self.assertFalse(server.started())
            if (call, status) == ("py:reconcile#1", 3):
                # 3 is reconcile's "committed, assignments remain held". It is not taken on
                # trust: the script goes on to count the holds itself, and they are all there.
                self.assert_nothing_replaced(server, result, status=3)
                self.assertIn("py:restore-status#2", server.calls())
                self.assertIn("RECOVERY INCOMPLETE: 3 assignment(s) are still held", result.stdout)
            elif call == "py:clock-recover#1":
                # Asked again, and with no further reading it stops with that status.
                self.assert_nothing_replaced(server, result, status=status)
            else:
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn(call, server.calls())
                self.assertNotIn("RESTORE COMPLETE", result.stderr)

        self.each([(call, status) for call in gates for status in ((1, 3) if call == "py:reconcile#1" else (3,))],
                  fail_with)

    def test_the_second_codex_reproduction_does_not_reach_the_start(self):
        # restore-hold returned 2 and reconcile returned 2, while clock-recover and verify returned 0.
        server = self.server()
        result = server.run(fail="py:restore-hold#1=2 py:reconcile#1=2")
        self.assert_nothing_replaced(server, result, status=2)
        self.assertIn("gate failed: hold every assignment", result.stderr)
        for never_reached in ("py:clock-recover#1", "py:reconcile#1", "py:verify#1", "mv#1"):
            self.assertNotIn(never_reached, server.calls())
        # And each of the two failures stops it alone.
        alone = self.server()
        self.assert_nothing_replaced(alone, alone.run(fail="py:reconcile#1=2"), status=2)
        self.assertNotIn("py:verify#1", alone.calls())

    def test_a_hold_step_that_reports_success_without_holding_is_caught(self):
        # restore-hold "succeeds" without having written anything: the next gate counts the holds itself.
        server = self.server()
        result = server.run(RESTORE_TEST_PRETEND="restore-hold")
        self.assert_nothing_replaced(server, result, status=1)
        self.assertIn("gate failed: every assignment is held for the restore", result.stderr)
        self.assertIn("The restore-hold step did not cover this database", result.stdout)

    def test_a_start_or_a_health_check_that_fails_does_not_leave_the_service_running(self):
        server = self.server()
        result = server.run(fail="systemctl-start#1=1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("the service did not start", result.stderr)
        self.assertIn("The service was stopped again.", result.stderr)
        self.assertIn("The restore itself is complete and the lock is released; the service is not running.",
                      result.stderr)
        self.assertIn(NOT_STARTED, result.stderr)
        self.assertFalse(server.locked())     # complete and gated: it may be served once the cause is fixed
        self.assertTrue(server.timer_active())   # and backed up: the state is whole

        server = self.server()
        result = server.run(fail="curl#1=22")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("the service was started but its health check failed", result.stderr)
        self.assertIn("The service was stopped again.", result.stderr)
        self.assertEqual(server.calls()[-4:], ["systemctl-start#1", "curl#1", "systemctl-stop#2", "timer-start#1"])
        self.assertEqual((server.active(), server.locked(), server.timer_active()), (False, False, True))
        self.assertNotIn("RESTORE COMPLETE", result.stderr)

    def test_a_move_that_cannot_be_taken_back_leaves_the_lock_and_the_database_that_was_live(self):
        # The vault cannot be set aside (mv#3), and the upload tree already put in place cannot be
        # moved back either (mv#4, the first step of taking it back).
        server = self.server()
        result = server.run(fail="mv#3=1 mv#4=1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(server.started())
        self.assertIn("Could not be taken back", result.stderr)
        self.assertIn("INTERRUPTED WHILE REPLACING the live uploads and vault. The live DATABASE was not replaced",
                      result.stderr)
        self.assertEqual((server.active(), server.locked(), server.timer_active()), (False, True, False))
        self.assertIn("in progress or was left unfinished", server.application_refuses())
        self.assertEqual(server.live_db.read_bytes(), server.original["data/questions.db"])
        self.assertTrue((server.state / "uploads/practical/restored.txt").exists())   # the snapshot's uploads,
        self.assertTrue((server.state / "vault/Generated/live.md").exists())          # the live vault

        again = server.run()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("That run was interrupted WHILE REPLACING", again.stderr)
        self.assert_restored(server, abandoned=1)
        self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)


class InterruptedRunTest(RestoreScriptTestCase):
    """Claude review, H1. The script dies in the middle (kill -9: no handler runs). Nothing completes
    what it left: the next run starts over, with every gate, against the live state as it then is."""

    def interrupted(self, at: str) -> tuple[Restore, str]:
        server = self.server()
        result = server.run(kill=at)
        self.assertEqual(result.returncode, -9, result.stdout + result.stderr)
        self.assertEqual(server.calls()[-1], at)
        self.assertFalse(server.started())
        left = server.staging() or server.replaced()
        return server, left[0].name.rsplit("-", 1)[1]

    def test_while_a_restore_is_unfinished_nothing_serves_or_backs_up_the_database(self):
        # "Reboot": nothing of the script is running any more. systemd would start the enabled unit.
        server, _stamp = self.interrupted("mv#2")
        self.assertEqual((server.active(), server.timer_active(), server.locked()), (False, False, True))
        refusal = server.application_refuses()
        self.assertIn("Refusing to start LearnPilot: a restore of this database is in progress or was left unfinished",
                      refusal)
        self.assertIn("run it again from the beginning", refusal)
        with self.assertRaises(production.ProductionConfigError) as caught:
            production.create_production_app({"DZI_DB": str(server.live_db)})   # before any other check
        self.assertEqual(str(caught.exception), refusal)
        with self.assertRaises(BackupError) as caught:
            server.backup()
        self.assertIn("no snapshot is taken of a partly replaced state", str(caught.exception))
        self.assertEqual(list(server.backups.iterdir()), [])
        # The units say the same to systemd, so that a reboot does not even try.
        lock = "/var/lib/learnpilot/data/" + lifecycle.RESTORE_LOCK_NAME
        for unit in ("learnpilot.service", "learnpilot-backup.service"):
            text = (ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
            self.assertIn(f"ConditionPathExists=!{lock}", text.split("[Service]")[0])

    def test_the_first_move_fails_the_service_comes_back_and_the_run_is_not_completed(self):
        """The reproduction of the review: staging passed its gates, the first move failed, the
        service became active again, an assignment was closed and a file uploaded, and then
        `--complete-swap` succeeded: the assignment reopened and the upload was gone."""
        server, stamp = self.interrupted("mv#1")
        # Somebody got the service going again, around the lock. Life went on in the live state.
        shutil.rmtree(server.lock)
        for flag in ("active", "timer-active"):
            (server.run_dir / flag).write_text("", encoding="utf-8")
        run_sql(server.live_db, "UPDATE quiz_assignments SET closed_at = ? WHERE id = ?",
                (utc(timedelta(minutes=-5)), self.unchanged))
        upload = server.state / "uploads/practical/handed-in-meanwhile.txt"
        upload.write_text("work handed in while the restore lay unfinished", encoding="utf-8")
        meanwhile = server.live_files()

        refused = server.run("--complete-swap", stamp)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("--complete-swap does not exist any more", refused.stderr)
        self.assertIn(UNTOUCHED, refused.stderr)
        self.assertEqual((server.live_files(), server.active()), (meanwhile, True))    # nothing overwritten

        # The only way on is the whole procedure again. It is made against the live state as it is now.
        server.original = meanwhile
        again = server.run()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("Left behind by an earlier run that did not finish", again.stderr)
        self.assert_restored(server, abandoned=1)
        self.assertIsNotNone(server.assignment(self.unchanged)["closed_at"])           # it did not reopen
        self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)
        self.assertIn(b"work handed in while the restore lay unfinished", server.everything())   # and nothing is gone
        self.assertEqual(server.holds(), {self.closed: None, self.unchanged: None, self.extended: None})

    def test_a_hand_made_staging_directory_and_marker_are_never_put_in_place(self):
        """The second reproduction: a staging directory with a raw snapshot and a hand-made marker
        saying that every gate passed with no assignment held."""
        server = self.server()
        forged = server.state / "restore-staging-20260101T000000Z"
        (forged / "uploads/practical").mkdir(parents=True)
        (forged / "vault").mkdir()
        shutil.copyfile(self.snapshot_db, forged / "questions.db")
        run_sql(forged / "questions.db", "UPDATE quiz_assignments SET title_bg = 'ПОДПРАВЕНО'")
        (forged / "uploads/practical/forged.txt").write_text("forged", encoding="utf-8")
        (forged / "gates-passed").write_text("0\n", encoding="utf-8")
        (server.state / "replaced-20260101T000000Z").mkdir()
        before = {path: path.read_bytes() for path in forged.rglob("*") if path.is_file()}

        for _attempt in range(2):                                   # and asking twice changes nothing
            refused = server.run("--complete-swap", "20260101T000000Z")
            self.assertEqual(refused.returncode, 2)
            self.assertIn(UNTOUCHED, refused.stderr)
            self.assertEqual((server.live_files(), server.active(), server.locked()), (server.original, True, False))

        result = server.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Left behind by an earlier run that did not finish: {forged}", result.stderr)
        self.assertIn("These copies are not used and not removed.", result.stderr)
        self.assert_restored(server, abandoned=1)
        self.assertEqual(run_sql(server.live_db, "SELECT COUNT(*) FROM quiz_assignments WHERE title_bg = 'ПОДПРАВЕНО'")
                         [0][0], 0)
        self.assertFalse((server.state / "uploads/practical/forged.txt").exists())
        self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)   # every gate was really run
        self.assertEqual({path: path.read_bytes() for path in forged.rglob("*") if path.is_file()}, before)

    def test_what_an_interrupted_run_left_staged_is_not_used_again_whatever_was_done_to_it(self):
        server, _stamp = self.interrupted("mv#2")
        (staging,) = server.staging()
        # The copy that had passed its gates, changed afterwards: the database and a file.
        run_sql(staging / "questions.db", "UPDATE quiz_assignments SET closed_at = NULL, deadline_at = NULL, "
                                          "lifecycle_hold = NULL, title_bg = 'ПРОМЕНЕНО'")
        (staging / "vault/Generated/restored.md").write_text("changed after it was checked", encoding="utf-8")
        changed = {path: path.read_bytes() for path in staging.rglob("*") if path.is_file()}

        again = server.run()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assert_restored(server, abandoned=1)
        self.assertEqual(run_sql(server.live_db, "SELECT COUNT(*) FROM quiz_assignments WHERE title_bg = 'ПРОМЕНЕНО'")
                         [0][0], 0)
        self.assertEqual((server.state / "vault/Generated/restored.md").read_text(encoding="utf-8"), "restored")
        self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)
        self.assertEqual({path: path.read_bytes() for path in staging.rglob("*") if path.is_file()}, changed)

    def test_wherever_the_replacement_is_cut_off_the_next_run_starts_over_and_loses_nothing(self):
        def cut_off(at):
            server, _stamp = self.interrupted(at)
            # Until the last rename, the database at the live path is the one that was live.
            self.assertEqual(server.live_db.read_bytes(), server.original["data/questions.db"])
            self.assertEqual((server.locked(), server.active(), server.timer_active()), (True, False, False))
            self.assertTrue((server.records / "swap-state").exists())       # where only root can change it
            again = server.run()
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertIn("An earlier restore did not finish", again.stderr)
            self.assertIn("That run was interrupted WHILE REPLACING", again.stderr)
            self.assert_restored(server, abandoned=1)
            self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)
            self.assertEqual(server.assignment(self.extended)["deadline_at"], self.later)
            self.assertEqual(server.holds(), {self.closed: None, self.unchanged: None, self.extended: None})

        self.each(("mv#1", "mv#2", "mv#3", "mv#4", "ln#1", "py:fingerprint#7", "mv#5"), cut_off)

    def test_cut_off_after_the_database_was_replaced_the_next_run_keeps_what_was_reconciled(self):
        server, _stamp = self.interrupted("py:verify#2")
        self.assertNotEqual(server.live_db.read_bytes(), server.original["data/questions.db"])
        self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)   # in place, gated,
        self.assertTrue(server.locked())                                                # and not served
        for _run in range(2):   # and once more after that: repeating a finished restore changes nothing either
            again = server.run()
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertEqual(server.assignment(self.closed)["closed_at"], self.closed_at)
            self.assertEqual(server.assignment(self.extended)["deadline_at"], self.later)
            self.assertEqual(server.holds(), {self.closed: None, self.unchanged: None, self.extended: None})
        self.assert_restored(server)
        self.assertEqual(len(server.replaced()), 3)

    def test_cut_off_before_the_replacement_the_lock_is_given_back_by_the_next_run_even_if_it_fails(self):
        server, _stamp = self.interrupted("py:reconcile#1")
        self.assertTrue(server.locked())
        self.assertFalse((server.records / "swap-state").exists())
        self.assertEqual(server.live_files(), server.original)
        # The next run fails too, cleanly. Nothing was ever replaced, so the lock goes and the timer returns.
        (abandoned,) = server.staging()
        result = server.run(fail="py:verify#1=1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("An earlier restore did not finish", result.stderr)
        self.assertIn(NOT_REPLACED, result.stderr)
        self.assertEqual((server.locked(), server.timer_active(), server.active()), (False, True, False))
        self.assertEqual(server.staging(), [abandoned])            # this run's copy removed, the other one kept
        self.assertEqual(server.live_files(), server.original)

    def test_after_an_interrupted_replacement_a_run_that_fails_keeps_the_lock(self):
        server, _stamp = self.interrupted("mv#3")
        result = server.run(fail="py:verify#1=1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("An EARLIER run was interrupted while it was replacing the live state, so the lock stays",
                      result.stderr)
        self.assertEqual((server.locked(), server.timer_active(), server.active()), (True, False, False))
        self.assertTrue((server.records / "swap-state").exists())
        self.assertIsNotNone(server.application_refuses())
        again = server.run()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assert_restored(server, abandoned=1)

    def test_a_second_restore_is_refused_while_one_is_running(self):
        server = self.server()
        running = subprocess.Popen(["bash", "-c", "sleep 60; :", "learnpilot-restore.sh"])
        self.addCleanup(running.wait)
        self.addCleanup(running.kill)
        server.lock.mkdir()
        server.records.mkdir(mode=0o700)       # who holds the lock is recorded where only root can write
        (server.records / "restore-owner").write_text(f"{running.pid}\n", encoding="utf-8")
        result = server.run()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"another restore is running (process {running.pid})", result.stderr)
        self.assertIn(UNTOUCHED, result.stderr)
        self.assertEqual((server.active(), server.live_files(), server.staging()), (True, server.original, []))
        self.assertTrue(server.locked())      # the other run's lock, not touched
        self.assertEqual((server.records / "restore-owner").read_text(encoding="utf-8"), f"{running.pid}\n")

    def test_nothing_in_the_lock_is_written_through_or_taken_as_its_holder(self):
        # Review of 39157f5: the lock sits in a directory the service can write, and root read and
        # wrote a pid file in it, through a symbolic link as well. The holder is recorded in the
        # restore's own records instead, and nothing is written into the lock but its own mark.
        server = self.server()
        running = subprocess.Popen(["bash", "-c", "sleep 60; :", "learnpilot-restore.sh"])
        self.addCleanup(running.wait)
        self.addCleanup(running.kill)
        server.lock.mkdir()
        sentinel = server.root / "sentinel"
        sentinel.write_bytes(f"{running.pid}\n".encode())                  # a running "restore", by its id
        server.lock.joinpath("pid").symlink_to(sentinel)
        result = server.run()
        self.assertEqual(result.returncode, 0, result.stderr)              # an abandoned lock: taken over
        self.assertIn("An earlier restore did not finish", result.stderr)
        self.assertEqual(sentinel.read_bytes(), f"{running.pid}\n".encode())
        self.assert_restored(server)


class BackupOverlapTest(RestoreScriptTestCase):
    """No backup is taken of a partly replaced state, and the timer comes back only at a safe end."""

    def test_the_timer_is_paused_for_the_restore_and_comes_back_when_it_has_finished(self):
        server = self.server()
        self.assertEqual(server.run().returncode, 0)
        calls = server.calls()
        self.assertLess(calls.index("timer-stop#1"), calls.index("systemctl-stop#1"))
        self.assertEqual(calls[-1], "timer-start#1")                       # after the start and its health check
        self.assertEqual(calls.count("timer-start#1"), 1)
        self.assertTrue(server.timer_active())
        self.assertTrue((server.backup() / "questions.db").is_file())      # and a backup works again

    def test_a_timer_that_was_not_active_is_not_started(self):
        server = self.server()
        (server.run_dir / "timer-active").unlink()
        self.assertEqual(server.run().returncode, 0)
        self.assertFalse([call for call in server.calls() if call.startswith("timer-")])
        self.assertFalse(server.timer_active())

    def test_a_restore_does_not_begin_while_a_backup_is_running(self):
        server = self.server()
        (server.run_dir / "backup-state").write_text("activating", encoding="utf-8")
        result = server.run()
        self.assert_untouched(server, result, status=1)
        self.assertIn("a backup is running (learnpilot-backup.service is activating). Wait until it has finished",
                      result.stderr)

    def test_a_backup_started_by_hand_that_spans_the_whole_restore_publishes_nothing(self):
        # Independent review of PR #326, low 1. systemd knows nothing of a backup started from a
        # shell, so the script goes ahead; and by the time that backup looks for the lock a second
        # time, the restore has finished and removed it. The database it copied is the one from
        # before the restore, and the paths it archives from now lead to the restored files.
        server = self.server()
        copy_the_database = backup_module.backup_database
        restores = []

        def copy_and_then_a_restore_runs(source, target):
            result = copy_the_database(source, target)
            restores.append(server.run())
            return result

        with mock.patch.object(backup_module, "backup_database", copy_and_then_a_restore_runs):
            with self.assertRaises(BackupError) as caught:
                server.backup()
        (restore,) = restores
        self.assertEqual(restore.returncode, 0, restore.stdout + restore.stderr)   # the restore went through,
        self.assert_restored(server)
        self.assertFalse(server.locked())                                          # and left no lock behind
        message = str(caught.exception)
        self.assertIn("the state changed hands while the snapshot was being made", message)
        for name in ("database", "uploads directory", "vault directory"):
            self.assertIn(f"the {name} was replaced by another", message)
        self.assertEqual(list(server.backups.iterdir()), [])                       # nothing published or staged
        snapshot = server.backup()                                                 # afterwards: the restored state
        with tarfile.open(snapshot / "files.tar.gz") as archive:
            self.assertIn("uploads/practical/restored.txt", archive.getnames())
            self.assertNotIn("uploads/practical/live.txt", archive.getnames())

    def test_a_running_backup_is_recognised_in_every_state_systemd_gives_it(self):
        # Claude review, N2. The backup unit is Type=oneshot: while it runs it is "activating", which
        # `systemctl is-active` does not report as active.
        self.assertIn("Type=oneshot", (ROOT / "deploy/systemd/learnpilot-backup.service").read_text(encoding="utf-8"))
        running = ("activating", "active", "reloading", "deactivating")

        def refused(state):
            server = self.server()
            (server.run_dir / "backup-state").write_text(state, encoding="utf-8")
            result = server.run()
            self.assert_untouched(server, result, status=1)
            if state in running:
                self.assertIn(f"a backup is running (learnpilot-backup.service is {state})", result.stderr)
            elif state == "ERROR":
                self.assertIn("the state of the backup service could not be read from systemd", result.stderr)
            else:
                self.assertIn(f"it cannot be told whether a backup is running: systemd reports '{state}'", result.stderr)

        self.each((*running, "", "maintenance", "something-new", "ERROR"), refused)

        def goes_ahead(state):
            server = self.server()
            (server.run_dir / "backup-state").write_text(state, encoding="utf-8")
            result = server.run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self.each(("inactive", "failed"), goes_ahead)

    def test_a_backup_that_starts_while_the_lock_is_being_taken_is_seen_once_it_is_held(self):
        # Between the first look and the lock a backup can start. With the lock held none can, so the
        # look that counts is made then, and there is nothing between the two for a backup to slip into.
        script = SCRIPT.read_text(encoding="utf-8")
        looks = [match.start() for match in re.finditer(r'show -p ActiveState "\$BACKUP_SERVICE"', script)]
        self.assertEqual(len(looks), 2)
        lock = script.index('sudo flock "$RECORDS" sh -c "$CHECKED$ACQUIRE"')
        self.assertLess(looks[0], lock)
        self.assertLess(lock, looks[1])
        self.assertLess(looks[1], script.index('gate "stop the service"'))

        server = self.server()
        hook = server.root / "a-backup-starts"
        hook.write_text('#!/bin/bash\necho activating > "$RESTORE_RUN/backup-state"\n', encoding="utf-8")
        hook.chmod(0o700)
        result = server.run(RESTORE_TEST_HOOK=f"timer-stop#1={hook}")     # after the first look, with the lock held
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("a backup is running (learnpilot-backup.service is activating)", result.stderr)
        self.assertIn(NOT_REPLACED, result.stderr)
        self.assertEqual((server.active(), server.locked(), server.timer_active()), (True, False, True))
        self.assertEqual(server.live_files(), server.original)
        self.assertFalse([call for call in server.calls() if call.startswith("systemctl-stop")])

    def test_the_service_and_the_timer_are_judged_by_their_state_too(self):
        # A service that systemd still reports as anything but inactive or failed has not stopped.
        for state in ("deactivating", "activating", "active", "", "ERROR"):
            with self.subTest(service_state_after_stop=state):
                server = self.server()
                hook = server.root / "state-after-stop"
                hook.write_text(f'#!/bin/bash\necho "{state}" > "$RESTORE_RUN/systemctl-state"\n', encoding="utf-8")
                hook.chmod(0o700)
                result = server.run(RESTORE_TEST_HOOK=f"systemctl-stop#1={hook}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("after it was stopped", result.stderr)
                self.assertIn(NOT_REPLACED, result.stderr)
                self.assertFalse(server.started())
                self.assertNotIn("py:restore-hold#1", server.calls())
        server = self.server()
        (server.run_dir / "timer-state").write_text("reloading", encoding="utf-8")
        result = server.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the state of the backup timer cannot be told", result.stderr)
        self.assertEqual((server.active(), server.locked()), (True, False))

    def test_after_an_interruption_the_timer_stays_off_until_a_restore_has_finished(self):
        server = self.server()
        self.assertEqual(server.run(kill="mv#4").returncode, -9)
        self.assertFalse(server.timer_active())
        self.assertTrue((server.records / "pending-backup-timer").exists())   # in the restore's own records
        self.assertFalse(server.lock.joinpath("backup-timer-was-active").exists())
        with self.assertRaises(BackupError):          # the timer could be started by hand, or by a reboot:
            server.backup()                           # the tool itself refuses
        self.assertEqual(server.run(fail="py:verify#1=1").returncode, 1)
        self.assertFalse(server.timer_active())       # a failed attempt is not a safe end
        self.assertEqual(server.run().returncode, 0)
        self.assertTrue(server.timer_active())        # the run that finished remembered that it had been on
        self.assertFalse((server.records / "pending-backup-timer").exists())
        self.assertEqual(server.calls()[-1], "timer-start#1")

    def test_a_failed_restart_of_the_timer_is_a_failed_run_and_the_next_run_starts_it(self):
        # Review of b719728: a failed restart ended with exit 0 and the note gone.
        server = self.server()
        result = server.run(fail="timer-start#1=1 timer-start#2=1")    # the restart, and finish's retry
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("WARNING: the backup timer could not be started again", result.stderr)
        self.assertNotIn("RESTORE COMPLETE", result.stderr)
        self.assertTrue(server.active())                                # the restore itself is in place
        self.assertEqual((server.timer_active(), server.locked()), (False, False))
        self.assertTrue((server.records / "pending-backup-timer").exists())
        earlier = len(server.calls())
        self.assertEqual(server.run().returncode, 0)
        calls = server.calls()[earlier:]                                # this run's: the log is kept
        self.assertLess(calls.index("timer-start#1"), calls.index("py:preflight#1"))   # first thing
        self.assertEqual(calls[-1], "timer-start#2")
        self.assertTrue(server.timer_active())
        self.assertFalse((server.records / "pending-backup-timer").exists())


class DocumentedCommandTest(RestoreScriptTestCase):
    """The command lines of docs/DEPLOYMENT.md, section 17, run as they are written there."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
        section = guide.split("## 17. Restore", 1)[1].split("## 18. Logs", 1)[0]
        cls.blocks = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)

    def documented(self, containing: str, server: Restore, **placeholders) -> str:
        (block,) = [block for block in self.blocks if containing in block and
                    ("--accept-held" in block) == ("--accept-held" in containing)]
        replacements = {"/opt/learnpilot/app/deploy/learnpilot-restore.sh": str(SCRIPT),
                        "/var/backups/learnpilot/snapshot-<stamp>": str(server.snapshot),
                        '"Your Full Name"': f'"{OPERATOR}"', **placeholders}
        for documented, here in replacements.items():
            self.assertIn(documented, block)
            block = block.replace(documented, here)
        return 'echo $$ > "$RESTORE_RUN/script.pid"\n' + block

    def test_the_documented_restore_command_runs_as_written(self):
        server = self.server()
        command = self.documented("--evidence-note", server)
        self.assertNotIn("--verified-utc", command)            # the script asks for it when it is used
        result = server.run_as_documented(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESTORE COMPLETE", result.stderr)
        self.assert_restored(server)
        # A name of three words, typed as the guide shows it, is one argument in every audit entry.
        self.assertEqual({entry["operator"] for entry in server.audit()}, {OPERATOR})
        self.assertEqual(len(server.audit()), 3)
        self.assertIn("why the database being replaced is still trusted", server.audit()[1]["evidence_note"])

    def test_the_documented_acknowledgement_of_two_held_assignments_runs_as_written(self):
        snapshot = self.variant(self.snapshot_db, "snapshot", "UPDATE quiz_assignments SET closed_at = ? WHERE id IN (?, ?)",
                                (self.closed_at, self.unchanged, self.extended))
        server = self.server(snapshot_db=snapshot)
        result = server.run_as_documented(self.documented("--accept-held 2", server))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESTORED, NOT RECOVERED: 2 assignment(s) stay held", result.stderr)
        held = [hold for hold in server.holds().values() if hold is not None]
        self.assertEqual(held, ["restore_pending,restore_unreconciled"] * 2)

    def test_the_documented_command_without_the_replaced_database_runs_as_written(self):
        server = self.server()
        command = self.documented("--replaced-database-unreadable --accept-held", server,
                                  **{"<number of assignments>": "3"})
        result = server.run_as_documented(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(server.holds().values()), {"restore_pending"})
        self.assertEqual({entry["operator"] for entry in server.audit()}, {OPERATOR})

    def test_every_documented_command_line_uses_only_options_the_script_has(self):
        usage = SCRIPT.read_text(encoding="utf-8").split("<<'USAGE'\n", 1)[1].split("\nUSAGE\n", 1)[0]
        known = set(re.findall(r"(?m)^\s+(--[a-z-]+)", usage)) | {"--suspect-to"}
        self.assertEqual(known, {"--snapshot", "--operator", "--evidence-note", "--replaced-database-unreadable",
                                 "--accept-held", "--suspect-from", "--suspect-to"})
        used = set()
        for block in self.blocks:
            used |= set(re.findall(r"(?<![\w-])--[a-z-]+", block))
        self.assertTrue(used)
        self.assertEqual(used - known, set())


if __name__ == "__main__":
    unittest.main()
