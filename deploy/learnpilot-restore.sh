#!/usr/bin/env bash
# LearnPilot: restore one snapshot behind the lifecycle gates.
# docs/DEPLOYMENT.md, section 17. Run it with bash, as an administrator who may use sudo:
#
#   bash /opt/learnpilot/app/deploy/learnpilot-restore.sh \
#       --snapshot /var/backups/learnpilot/snapshot-<stamp> \
#       --operator "Your Full Name" \
#       --evidence-note "why the database being replaced can be trusted"
#
# What it guarantees:
#
#   * Nothing is modified, and the service keeps running, until the pre-flight
#     checks have passed.
#   * From then until the restore has finished or was undone, a lock sits next to
#     the database. The application refuses to start while it is there, after a
#     reboot as well, and no backup is taken.
#   * One command starts the service again, the last one, and it is reached only
#     when every gate before it succeeded. There is no other start in this file.
#   * Every mandatory step is a gate. Its exit status is read explicitly; anything
#     but success ends the script without a start. No mandatory command runs in a
#     pipeline, and none runs inside a shell function.
#   * Everything is prepared and checked on a copy. The live uploads and vault are
#     replaced after that, and the live database last, by one rename. Until that
#     rename the database at the live path is the one that was live, so a run that
#     stops anywhere before it can be repeated from the beginning: the next run
#     reconciles against the same, true, latest state.
#   * Nothing on disk is ever taken as proof that a check was carried out. There
#     is no way to finish an interrupted run from what it left behind: its copy is
#     abandoned and the restore starts again, with every gate.
#   * Within one run, what is replaced is byte for byte what was examined, and
#     what is put in place is byte for byte what was verified: the script holds
#     the digests in memory and compares them just before and just after.
#   * It deletes nothing but the copy it made itself. What was live is kept.
#   * An assignment that is still held for the restore is never reported as
#     recovered.
#
# It releases no hold. Only `manage_lifecycle reconcile`, from the database being
# replaced, does that.
#
# Registration (docs/reviews/EMAIL_REGISTRATION_DESIGN.md, 3.9, decision D20 (a)): the
# registration cleanup timer is stopped and its service is required to be idle before
# the live database is read; on the staged copy every pending registration is voided
# and registration is held; the hold is never released by this script, whatever
# happens. The operator ends it with `manage_accounts registrations-release-hold`.

if [ -z "${BASH_VERSION:-}" ]; then
  echo "restore: run this script with bash" >&2
  exit 2
fi
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  echo "restore: run this script, do not source it" >&2
  return 2
fi

set -Eeuo pipefail

STATE=${LEARNPILOT_STATE:-/var/lib/learnpilot}
APP=${LEARNPILOT_APP:-/opt/learnpilot/app}
PYTHON=${LEARNPILOT_PYTHON:-/opt/learnpilot/venv/bin/python}
SERVICE=${LEARNPILOT_SERVICE:-learnpilot}
BACKUP_SERVICE=${LEARNPILOT_BACKUP_SERVICE:-learnpilot-backup.service}
BACKUP_TIMER=${LEARNPILOT_BACKUP_TIMER:-learnpilot-backup.timer}
CLEANUP_SERVICE=${LEARNPILOT_CLEANUP_SERVICE:-learnpilot-registration-cleanup.service}
CLEANUP_TIMER=${LEARNPILOT_CLEANUP_TIMER:-learnpilot-registration-cleanup.timer}
RUN_AS=${LEARNPILOT_USER:-learnpilot}
RUN_GROUP=${LEARNPILOT_GROUP:-learnpilot}
HEALTH_URL=${LEARNPILOT_HEALTH_URL:-http://127.0.0.1:8000/healthz}
AUDIT_LOG=${LEARNPILOT_AUDIT_LOG:-$STATE/lifecycle-operator.log}
LIVE_DB=$STATE/data/questions.db
LOCK=$STATE/data/restore-in-progress   # web.services.lifecycle.RESTORE_LOCK_NAME
case $STATE in
  /*) ;;
  *)
    echo "restore: LEARNPILOT_STATE has to be an absolute path" >&2
    exit 2
    ;;
esac
case $STATE in
  *"|"* | *[[:space:]]*)
    echo "restore: LEARNPILOT_STATE must not contain a space or a vertical bar" >&2
    exit 2
    ;;
esac

SNAPSHOT=
OPERATOR=
EVIDENCE_NOTE=
SUSPECT_FROM=
SUSPECT_TO=
ACCEPT_HELD=0
REPLACED_UNREADABLE=no

PHASE=untouched      # untouched -> locked -> swap -> committed -> started
LOCK_HELD=no
INTERRUPTED_BEFORE=no
TIMER_WAS_ACTIVE=no
CLEANUP_TIMER_WAS_ACTIVE=no
START_ATTEMPTED=no
STAMP=
STAGE=
STAGED_DB=
REPLACED=
LIVE_SEEN=           # digests, held here and nowhere else: the live database as it was examined,
FILES_STAGED=        # the files as they were extracted,
VERIFIED=            # the copy as it was verified
FAILURE=
HINT=
UNDO=()              # "move|WHERE IT WAS|WHERE IT GOES" or "link|FILE|SECOND NAME", written BEFORE each step

# Move one thing to a place where there is nothing yet. `mv` alone would move
# into a directory that is in the way, or over a file.
MOVE='set -eu
if [ -e "$2" ] || [ -L "$2" ]; then
  echo "restore: in the way: $2" >&2
  exit 1
fi
mv -- "$1" "$2"'

# Take one move back, going by where things are and not by whether the move was
# believed to have happened: a step can be cut off after its rename and before
# anything is noted down.
UNMOVE='set -eu
if [ -e "$1" ] || [ -L "$1" ]; then
  exit 0             # it is where it was: that move never happened
fi
if [ -e "$2" ] || [ -L "$2" ]; then
  mv -- "$2" "$1"
  exit 0
fi
echo "restore: neither where it was nor where it was being moved to: $1" >&2
exit 1'

# Remove the second name of the live database, and only if it is one: the same
# file as the one at the live path. Once the database has been replaced, that
# name is the only one the database that was live still has.
UNLINK='set -eu
if [ ! -e "$2" ] && [ ! -L "$2" ]; then
  exit 0             # no second name was made
fi
if [ "$1" -ef "$2" ]; then
  rm -f -- "$2"
  exit 0
fi
echo "restore: kept: it is not a second name of the database at the live path: $2" >&2
exit 1'

# The service's settings, read without showing any of them: each name is set
# exactly once in the environment file, as plain NAME=value, to the path given.
SETTINGS='set -eu
file=$1
shift
for wanted in "$@"; do
  name=${wanted%%=*}
  count=$(grep -cE "^[[:space:]]*(export[[:space:]]+)?$name[[:space:]]*=" "$file") || [ "$count" = 0 ]
  if [ "$count" != 1 ]; then
    echo "restore: $name is set $count times in $file; it has to be set exactly once" >&2
    exit 1
  fi
  if ! grep -qxF -- "$wanted" "$file"; then
    echo "restore: $name in $file is not ${wanted#*=}, written as plain $name=PATH" >&2
    exit 1
  fi
done'

say() {
  printf 'restore: %s\n' "$*" >&2 || return 0   # a terminal that has gone away stops nothing
}

usage() {
  cat >&2 <<'USAGE'
Usage: bash learnpilot-restore.sh --snapshot DIR --operator "FULL NAME"
           (--evidence-note "TEXT" | --replaced-database-unreadable)
           [--accept-held N]
           [--suspect-from "YYYY-MM-DD HH:MM:SS" --suspect-to "YYYY-MM-DD HH:MM:SS"]

  --snapshot DIR       A complete snapshot: DIR/questions.db and DIR/files.tar.gz.
  --operator NAME      Who runs this. Recorded in the lifecycle audit log.
  --evidence-note TEXT Why the database being replaced can be trusted as the
                       latest lifecycle state (what made the restore necessary).
  --replaced-database-unreadable
                       The database being replaced cannot be used as evidence.
                       Nothing is reconciled: every assignment stays held.
  --accept-held N      Restore although exactly N assignments stay held and
                       closed to everyone. Releases nothing. Default 0.
  --suspect-from, --suspect-to
                       UTC interval in which the server clock was wrong, if
                       there was a clock incident. Passed to clock-recover.

The current UTC time is asked for when it is used, never taken in advance.
An interrupted run is not completed: run the script again from the beginning.
USAGE
}

die() {              # die STATUS MESSAGE: the only way this script gives up
  FAILURE=$2
  exit "$1"
}

gate() {             # gate "WHAT IT ESTABLISHES" COMMAND [ARGUMENT...]
  # COMMAND is always an external program (or the builtin cd or test), never a
  # shell function: the status tested here is then the status of the step itself.
  local what=$1 status=0
  shift
  say "gate: $what"
  "$@" || status=$?
  if [ "$status" -ne 0 ]; then
    die "$status" "gate failed: $what (exit status $status)"
  fi
}

restart_timers() {   # after the service has started: each timer that was active, once, even when interrupted
  # The flag of a timer is cleared only after its start command has returned, so a signal
  # that cuts this short leaves the other timer (and the interrupted one) still pending
  # for finish, which calls this again.
  if [ "$TIMER_WAS_ACTIVE" = yes ]; then
    if sudo systemctl start "$BACKUP_TIMER"; then
      say "The backup timer is active again."
    else
      say "WARNING: the backup timer could not be started again. Start it by hand: $BACKUP_TIMER"
    fi
    TIMER_WAS_ACTIVE=no
  fi
  if [ "$CLEANUP_TIMER_WAS_ACTIVE" = yes ]; then
    if sudo systemctl start "$CLEANUP_TIMER"; then
      say "The registration cleanup timer is active again."
    else
      say "WARNING: the registration cleanup timer could not be started again. Start it by hand: $CLEANUP_TIMER"
    fi
    CLEANUP_TIMER_WAS_ACTIVE=no
  fi
}

finish() {
  local status=$? index entry kind rest undone at_live lock
  trap - EXIT
  trap '' HUP INT QUIT TERM      # what follows is not to be cut short in its turn
  if [ "$status" -eq 0 ] && [ "$PHASE" = started ]; then
    exit 0
  fi
  if [ "$status" -eq 0 ]; then
    status=1         # ended without a failure and without a start: not a success
  fi
  say "------------------------------------------------------------------"
  say "STOPPED: ${FAILURE:-the script ended early}"
  if [ -n "$HINT" ]; then
    say "$HINT"
  fi

  if [ "$PHASE" = untouched ]; then
    if [ "$LOCK_HELD" = maybe ]; then
      # Stopped in the instant the lock was being made. An empty lock is this run's
      # (another run's has its owner written in it), and rmdir removes nothing else.
      if sudo test -d "$LOCK" && ! sudo rmdir -- "$LOCK"; then
        say "A restore lock is there that this run did not make: $LOCK"
      fi
    fi
    say "Nothing was touched, and the service was left as it was. Exit status $status."
    exit "$status"
  fi
  if [ "$PHASE" = started ]; then
    say "The service is running on the restored state: the script was stopped after it had started it."
    # The lock is gone, and with it the note of which timers were active; the intent is
    # kept in TIMER_WAS_ACTIVE / CLEANUP_TIMER_WAS_ACTIVE until a timer has been started.
    restart_timers
    say "Check that the backup timer ($BACKUP_TIMER) and the registration cleanup timer ($CLEANUP_TIMER) are active. Exit status $status."
    exit "$status"
  fi

  at_live=
  if [ "$PHASE" = swap ]; then
    # The replacement had begun. How far it got is read off the disk, where the copy
    # is, byte for byte: at the live path (the database was replaced), or still in
    # staging (it was not). A step can be cut off after its rename and before this
    # script has noted anything, so what the script believes decides nothing here.
    at_live=unknown
    if "${FINGERPRINT[@]}" "$LIVE_DB" --expect "$VERIFIED" > /dev/null; then
      at_live=copy
    elif "${FINGERPRINT[@]}" "$STAGED_DB" --expect "$VERIFIED" > /dev/null; then
      at_live=original   # whatever is at the live path, it is not the copy: that never left staging
    fi

    case $at_live in
      copy)
        # Replaced. Nothing is taken back: the files went in before it, and what was
        # set aside is the only place the database that was live still is.
        PHASE=committed
        say "The database HAD BEEN REPLACED when the script was stopped: the copy that passed every gate"
        say "is at the live path."
        ;;
      original)
        # Not replaced. Take back what was moved, newest first.
        undone=yes
        index=${#UNDO[@]}
        while [ "$index" -gt 0 ]; do
          index=$((index - 1))
          entry=${UNDO[$index]}
          kind=${entry%%|*}
          rest=${entry#*|}
          case $kind in
            move)
              if ! sudo sh -c "$UNMOVE" sh "${rest%%|*}" "${rest#*|}"; then
                undone=no
              fi
              ;;
            link)
              if ! sudo sh -c "$UNLINK" sh "${rest%%|*}" "${rest#*|}"; then
                undone=no
              fi
              ;;
            *)
              undone=no
              ;;
          esac
          if [ "$undone" = no ]; then
            say "Could not be taken back: ${rest#*|}"
            break
          fi
        done
        if [ "$undone" = yes ]; then
          # The mark of an earlier interrupted run is not this run's to remove.
          if [ "$INTERRUPTED_BEFORE" = yes ] || sudo rm -f -- "$LOCK/swap-started"; then
            PHASE=locked
            say "The replacement had begun and was taken back completely."
          fi
        fi
        ;;
      *)
        say "WHETHER THE DATABASE WAS REPLACED COULD NOT BE ESTABLISHED: the copy that was verified is,"
        say "byte for byte, neither at the live path nor in staging any more. Nothing was moved back."
        ;;
    esac
  fi

  case $PHASE in
    locked)
      say "Nothing was replaced: the live database, uploads and vault are exactly as they were."
      case $STAGE in
        "$STATE"/restore-staging-*)
          if sudo test -e "$STAGE"; then
            if sudo rm -rf -- "$STAGE"; then
              say "The copy this run made was removed: $STAGE"
            else
              say "The copy this run made could not be removed: $STAGE"
            fi
          fi
          ;;
      esac
      case $REPLACED in
        "$STATE"/replaced-*)
          # Made for this run and empty again: nothing is kept in it.
          if sudo test -d "$REPLACED" && ! sudo rmdir -- "$REPLACED"; then
            say "Not empty, left in place: $REPLACED"
          fi
          ;;
      esac
      if [ "$INTERRUPTED_BEFORE" = yes ]; then
        say "An EARLIER run was interrupted while it was replacing the live state, so the lock stays:"
        say "the service does not start, and no backup is taken, until a restore has finished."
        say "The registration cleanup timer stays stopped as well (it refuses while the lock exists)."
      elif [ "$LOCK_HELD" = yes ]; then
        if sudo rm -rf -- "$LOCK"; then
          say "The restore lock was released."
          if [ "$TIMER_WAS_ACTIVE" = yes ] && ! sudo systemctl start "$BACKUP_TIMER"; then
            say "WARNING: the backup timer could not be started again: $BACKUP_TIMER"
          fi
          if [ "$CLEANUP_TIMER_WAS_ACTIVE" = yes ] && ! sudo systemctl start "$CLEANUP_TIMER"; then
            say "WARNING: the registration cleanup timer could not be started again: $CLEANUP_TIMER"
          fi
        else
          say "The restore lock could not be removed: $LOCK"
        fi
      fi
      say "After correcting the cause, run the script again from the beginning."
      ;;
    swap)
      if [ "$at_live" = original ] && [ "$REPLACED_UNREADABLE" = no ]; then
        say "INTERRUPTED WHILE REPLACING the live uploads and vault. The live DATABASE was not replaced:"
        say "it is still the one that was live."
      elif [ "$at_live" = original ]; then
        # It was being set aside as it was, and could not be put back whole.
        say "INTERRUPTED WHILE REPLACING the live state. The copy was NOT put in place as the live database."
        say "The database that was live, and whatever belongs to it, is at its path or in"
        say "$REPLACED, and nowhere else."
      else
        say "INTERRUPTED WHILE REPLACING the live state."
      fi
      say "What was set aside is in $REPLACED; the rest of the copy is in $STAGE."
      say "The lock stays: the service does not start and no backup is taken."
      say "The registration cleanup timer stays stopped until a restore has finished."
      say "Do not move anything by hand and do not remove the lock. After correcting the cause, run"
      say "the script again from the beginning. It starts over, with every gate, from the snapshot."
      ;;
    committed)
      say "The restored state is in place. It had passed every gate before it was put there."
      say "What it replaced is in $REPLACED."
      if [ "$START_ATTEMPTED" = yes ]; then
        # Whatever became of the start: a script that ends like this leaves no service running.
        if sudo systemctl stop "$SERVICE"; then
          say "The service was stopped again."
        else
          say "WARNING: the service could not be stopped; check it by hand: $SERVICE"
        fi
      fi
      lock=0
      sudo sh -c 'if [ -e "$1" ] || [ -L "$1" ]; then exit 10; fi; exit 11' sh "$LOCK" || lock=$?
      if [ "$lock" -eq 11 ]; then
        say "The restore itself is complete and the lock is released; the service is not running."
        if [ "$TIMER_WAS_ACTIVE" = yes ] && ! sudo systemctl start "$BACKUP_TIMER"; then
          say "WARNING: the backup timer could not be started again: $BACKUP_TIMER"
        fi
        if [ "$CLEANUP_TIMER_WAS_ACTIVE" = yes ] && ! sudo systemctl start "$CLEANUP_TIMER"; then
          say "WARNING: the registration cleanup timer could not be started again: $CLEANUP_TIMER"
        fi
      else
        say "The lock stays. Run the script again from the beginning."
      fi
      ;;
  esac
  say "THE SERVICE WAS NOT STARTED. Exit status $status."
  exit "$status"
}

trap finish EXIT
trap 'FAILURE=${FAILURE:-"a command failed at line $LINENO: $BASH_COMMAND"}' ERR
trap '' PIPE
trap 'FAILURE="interrupted (SIGHUP)"; exit 129' HUP
trap 'FAILURE="interrupted (SIGINT)"; exit 130' INT
trap 'FAILURE="interrupted (SIGQUIT)"; exit 131' QUIT
trap 'FAILURE="interrupted (SIGTERM)"; exit 143' TERM

# --- arguments ---------------------------------------------------------------

while [ $# -gt 0 ]; do
  case $1 in
    --snapshot | --operator | --evidence-note | --accept-held | --suspect-from | --suspect-to)
      if [ $# -lt 2 ]; then
        usage
        die 2 "$1 needs a value"
      fi
      case $1 in
        --snapshot) SNAPSHOT=$2 ;;
        --operator) OPERATOR=$2 ;;
        --evidence-note) EVIDENCE_NOTE=$2 ;;
        --accept-held) ACCEPT_HELD=$2 ;;
        --suspect-from) SUSPECT_FROM=$2 ;;
        --suspect-to) SUSPECT_TO=$2 ;;
      esac
      shift 2
      ;;
    --replaced-database-unreadable)
      REPLACED_UNREADABLE=yes
      shift
      ;;
    --complete-swap)
      die 2 "--complete-swap does not exist any more. An interrupted run is never completed from what it left on disk: nothing there proves that its checks were made, or that the live state is still the one they were made against. Run the script again from the beginning."
      ;;
    --verified-utc)
      die 2 "--verified-utc is not taken on the command line: a reading given in advance is stale by the time it is used. The script asks for it."
      ;;
    -h | --help)
      usage
      trap - EXIT
      exit 0
      ;;
    *)
      usage
      die 2 "unknown argument: $1"
      ;;
  esac
done

if [ -z "$SNAPSHOT" ]; then
  usage
  die 2 "--snapshot is required"
fi
case $OPERATOR in
  *[![:space:]]*) ;;
  *)
    usage
    die 2 "--operator is required: the full name of the person running the restore"
    ;;
esac
case $ACCEPT_HELD in
  '' | *[!0-9]*) die 2 "--accept-held takes a number of assignments" ;;
esac
if [ "$REPLACED_UNREADABLE" = yes ]; then
  if [ -n "$EVIDENCE_NOTE" ]; then
    die 2 "--evidence-note and --replaced-database-unreadable exclude each other"
  fi
else
  case $EVIDENCE_NOTE in
    *[![:space:]]*) ;;
    *) die 2 "--evidence-note is required (or --replaced-database-unreadable, which reconciles nothing)" ;;
  esac
fi
if [ -n "$SUSPECT_FROM" ] && [ -z "$SUSPECT_TO" ]; then
  die 2 "--suspect-from and --suspect-to are given together"
fi
if [ -z "$SUSPECT_FROM" ] && [ -n "$SUSPECT_TO" ]; then
  die 2 "--suspect-from and --suspect-to are given together"
fi

AS_SERVICE=(sudo -u "$RUN_AS")
AUDIT=(--operator "$OPERATOR" --audit-log "$AUDIT_LOG")
FINGERPRINT=("${AS_SERVICE[@]}" "$PYTHON" -m src.restore_preflight --fingerprint)
RECOVER=(clock-recover)
if [ -n "$SUSPECT_FROM" ]; then
  RECOVER=(clock-recover --suspect-from "$SUSPECT_FROM" --suspect-to "$SUSPECT_TO")
fi

# --- 1. pre-flight: nothing is modified and the service keeps running --------------

for tool in sudo systemctl sqlite3 tar install mv ln rm mkdir rmdir cat grep ps curl date sleep; do
  if ! command -v "$tool" > /dev/null; then
    die 2 "a program this script needs is not installed: $tool"
  fi
done
gate "the application checkout is the working directory" cd "$APP"
gate "the operator tooling is in the checkout" test -f src/manage_lifecycle.py
gate "sudo can be used" sudo true
gate "the application's Python can be run as the service user" "${AS_SERVICE[@]}" "$PYTHON" -c 'import sqlite3, tarfile'
gate "the snapshot is complete and there is room and one filesystem for the copy" \
  "${AS_SERVICE[@]}" "$PYTHON" -m src.restore_preflight --snapshot "$SNAPSHOT" --state-root "$STATE"
# What the service really uses, asked of systemd and not assumed: one environment
# file, nothing overriding it in the unit, and in that file each path set exactly
# once, to what this script is about to replace. Nothing of the file is shown.
HINT="The script replaces what is under $STATE. If the service uses other paths, set LEARNPILOT_STATE; see docs/DEPLOYMENT.md, section 17."
UNIT_FILES=$(sudo systemctl show -p EnvironmentFiles "$SERVICE") \
  || die "$?" "the settings of the service could not be read from systemd ($SERVICE)"
case $UNIT_FILES in
  *"
"*) die 1 "the service reads more than one environment file: which paths it uses is ambiguous ($SERVICE)" ;;
  "EnvironmentFiles=/"*" (ignore_errors=no)") ;;
  *) die 1 "the service has no single, mandatory environment file: which paths it uses cannot be told ($SERVICE)" ;;
esac
ENV_FILE=${UNIT_FILES#EnvironmentFiles=}
ENV_FILE=${ENV_FILE% (ignore_errors=no)}
UNIT_SETTINGS=$(sudo systemctl show -p Environment "$SERVICE") \
  || die "$?" "the settings of the service could not be read from systemd ($SERVICE)"
case $UNIT_SETTINGS in
  *DZI_DB=* | *DZI_UPLOAD_ROOT=* | *DZI_VAULT=*)
    die 1 "a state path is set in the unit itself (Environment=) as well as in $ENV_FILE: which one the service uses is ambiguous"
    ;;
esac
gate "the service is set, once and plainly, to the paths this restore replaces" sudo sh -c "$SETTINGS" sh \
  "$ENV_FILE" "DZI_DB=$LIVE_DB" "DZI_UPLOAD_ROOT=$STATE/uploads/practical" "DZI_VAULT=$STATE/vault"
HINT=
if [ "$REPLACED_UNREADABLE" = no ]; then
  HINT="If there is no usable database to replace, see --replaced-database-unreadable."
  gate "the database to be replaced exists" sudo test -f "$LIVE_DB"
  HINT=
fi
gate "the audit log can be written by the service user" "${AS_SERVICE[@]}" sh -c ': >> "$1"' sh "$AUDIT_LOG"
# A backup that is running is "activating" to systemd for as long as it runs (the
# unit is Type=oneshot), which `is-active` does not count as active: the state is
# read as such. This first look only saves a wasted start; the one that counts is
# made once the lock is held.
BACKUP_STATE=$(sudo systemctl show -p ActiveState "$BACKUP_SERVICE") \
  || die "$?" "the state of the backup service could not be read from systemd ($BACKUP_SERVICE)"
case $BACKUP_STATE in
  ActiveState=inactive | ActiveState=failed) ;;
  ActiveState=activating | ActiveState=active | ActiveState=reloading | ActiveState=deactivating)
    die 1 "a backup is running ($BACKUP_SERVICE is ${BACKUP_STATE#ActiveState=}). Wait until it has finished, then run the script again"
    ;;
  *)
    die 1 "it cannot be told whether a backup is running: systemd reports '${BACKUP_STATE#ActiveState=}' for $BACKUP_SERVICE"
    ;;
esac
# The same first look at the registration cleanup service: it only saves a wasted start. A
# service systemd does not know is reported "inactive", which is right: nothing runs.
CLEANUP_STATE=$(sudo systemctl show -p ActiveState "$CLEANUP_SERVICE") \
  || die "$?" "the state of the registration cleanup service could not be read from systemd ($CLEANUP_SERVICE)"
case $CLEANUP_STATE in
  ActiveState=inactive | ActiveState=failed) ;;
  ActiveState=activating | ActiveState=active | ActiveState=reloading | ActiveState=deactivating)
    die 1 "a registration cleanup is running ($CLEANUP_SERVICE is ${CLEANUP_STATE#ActiveState=}). Wait until it has finished, then run the script again"
    ;;
  *)
    die 1 "it cannot be told whether a registration cleanup is running: systemd reports '${CLEANUP_STATE#ActiveState=}' for $CLEANUP_SERVICE"
    ;;
esac
STAMP=$(date -u +%Y%m%dT%H%M%SZ) || die 1 "the date could not be read"
WAITED=0
while sudo test -e "$STATE/restore-staging-$STAMP" || sudo test -e "$STATE/replaced-$STAMP"; do
  # An earlier run was begun in this very second. Its directories are not this run's.
  WAITED=$((WAITED + 1))
  if [ "$WAITED" -gt 5 ]; then
    die 1 "a restore with the stamp $STAMP already exists; run the script again"
  fi
  sleep 1
  STAMP=$(date -u +%Y%m%dT%H%M%SZ) || die 1 "the date could not be read"
done
ABANDONED=$(sudo sh -c 'for copy in "$1"/restore-staging-*; do
  if [ -e "$copy" ]; then
    printf " %s" "$copy"
  fi
done' sh "$STATE") || die "$?" "the state directory could not be listed: $STATE"
if [ -n "$ABANDONED" ]; then
  say "Left behind by an earlier run that did not finish:$ABANDONED"
  say "These copies are not used and not removed. Delete them deliberately when they are not needed."
fi

# --- 2. the lock, then backups paused and the service stopped -------------------------

LOCK_HELD=maybe          # until the next lines have run, the lock may or may not be this run's (finish)
if ! sudo mkdir -- "$LOCK"; then
  LOCK_HELD=no
  if ! sudo test -d "$LOCK"; then
    die 1 "the restore lock could not be made: $LOCK"
  fi
  OWNER=$(sudo cat -- "$LOCK/pid") || OWNER=
  case $OWNER in
    '' | *[!0-9]*) ;;
    *)
      if RUNNING=$(sudo ps -p "$OWNER" -o args=); then
        case $RUNNING in
          *learnpilot-restore*) die 1 "another restore is running (process $OWNER). Nothing was touched by this one" ;;
        esac
      fi
      ;;
  esac
  say "An earlier restore did not finish: its lock is still there ($LOCK). This run takes it over."
  if sudo test -e "$LOCK/swap-started"; then
    INTERRUPTED_BEFORE=yes
    say "That run was interrupted WHILE REPLACING the live uploads and vault. This run starts over"
    say "from the snapshot; what the earlier run set aside stays where it is."
  fi
  if sudo test -e "$LOCK/backup-timer-was-active"; then
    TIMER_WAS_ACTIVE=yes
  fi
  if sudo test -e "$LOCK/registration-cleanup-timer-was-active"; then
    CLEANUP_TIMER_WAS_ACTIVE=yes
  fi
fi
LOCK_HELD=yes
PHASE=locked
gate "record this run in the restore lock" sudo sh -c 'printf "%s\n" "$2" > "$1"' sh "$LOCK/pid" "$$"

TIMER_STATE=$(sudo systemctl show -p ActiveState "$BACKUP_TIMER") \
  || die "$?" "the state of the backup timer could not be read from systemd ($BACKUP_TIMER)"
case $TIMER_STATE in
  ActiveState=inactive | ActiveState=failed) ;;
  ActiveState=active | ActiveState=activating)
    gate "note that the backup timer was active" sudo sh -c ': > "$1"' sh "$LOCK/backup-timer-was-active"
    TIMER_WAS_ACTIVE=yes
    gate "pause the backup timer" sudo systemctl stop "$BACKUP_TIMER"
    ;;
  *) die 1 "the state of the backup timer cannot be told: systemd reports '${TIMER_STATE#ActiveState=}' for $BACKUP_TIMER" ;;
esac
# With the lock held no backup starts any more (the unit's condition, and the tool
# itself). One that was started before the lock is still running: this is the look
# that counts, and there is nothing between it and the lock for a backup to slip into.
BACKUP_STATE=$(sudo systemctl show -p ActiveState "$BACKUP_SERVICE") \
  || die "$?" "the state of the backup service could not be read from systemd ($BACKUP_SERVICE)"
case $BACKUP_STATE in
  ActiveState=inactive | ActiveState=failed) ;;
  ActiveState=activating | ActiveState=active | ActiveState=reloading | ActiveState=deactivating)
    die 1 "a backup is running ($BACKUP_SERVICE is ${BACKUP_STATE#ActiveState=}). Wait until it has finished, then run the script again"
    ;;
  *)
    die 1 "it cannot be told whether a backup is running: systemd reports '${BACKUP_STATE#ActiveState=}' for $BACKUP_SERVICE"
    ;;
esac

# The registration cleanup writes to the live database, so it is stopped as well, and for
# the same reason as the backup: before the live database is checkpointed and fingerprinted.
# Stopping the timer does not stop a run that has begun, so the service is looked at once
# the timer is stopped and the lock is held (the command refuses to start under the lock).
CLEANUP_TIMER_STATE=$(sudo systemctl show -p ActiveState "$CLEANUP_TIMER") \
  || die "$?" "the state of the registration cleanup timer could not be read from systemd ($CLEANUP_TIMER)"
case $CLEANUP_TIMER_STATE in
  ActiveState=inactive | ActiveState=failed) ;;
  ActiveState=active | ActiveState=activating)
    gate "note that the registration cleanup timer was active" \
      sudo sh -c ': > "$1"' sh "$LOCK/registration-cleanup-timer-was-active"
    CLEANUP_TIMER_WAS_ACTIVE=yes
    gate "pause the registration cleanup timer" sudo systemctl stop "$CLEANUP_TIMER"
    ;;
  *) die 1 "the state of the registration cleanup timer cannot be told: systemd reports '${CLEANUP_TIMER_STATE#ActiveState=}' for $CLEANUP_TIMER" ;;
esac
CLEANUP_STATE=$(sudo systemctl show -p ActiveState "$CLEANUP_SERVICE") \
  || die "$?" "the state of the registration cleanup service could not be read from systemd ($CLEANUP_SERVICE)"
case $CLEANUP_STATE in
  ActiveState=inactive | ActiveState=failed) ;;
  ActiveState=activating | ActiveState=active | ActiveState=reloading | ActiveState=deactivating)
    die 1 "a registration cleanup is running ($CLEANUP_SERVICE is ${CLEANUP_STATE#ActiveState=}). Wait until it has finished, then run the script again"
    ;;
  *)
    die 1 "it cannot be told whether a registration cleanup is running: systemd reports '${CLEANUP_STATE#ActiveState=}' for $CLEANUP_SERVICE"
    ;;
esac

gate "stop the service" sudo systemctl stop "$SERVICE"
SERVICE_STATE=$(sudo systemctl show -p ActiveState "$SERVICE") \
  || die "$?" "gate failed: the state of the service could not be read from systemd after it was stopped"
case $SERVICE_STATE in
  ActiveState=inactive | ActiveState=failed) ;;
  *) die 1 "gate failed: the service is still active after it was stopped (systemd reports '${SERVICE_STATE#ActiveState=}')" ;;
esac

# The database being replaced is written out into its main file (what a clean stop
# does anyway), and its bytes are remembered: they are examined below, and they have
# to be the same bytes when the database is set aside.
if [ "$REPLACED_UNREADABLE" = no ]; then
  HINT="If the database being replaced cannot be used at all, see --replaced-database-unreadable."
  gate "write the database being replaced out completely" \
    "${AS_SERVICE[@]}" sqlite3 "$LIVE_DB" 'PRAGMA wal_checkpoint(TRUNCATE);'
  gate "the database being replaced is complete in its main file" sudo sh -c '
    set -eu
    if [ -s "$1"-wal ]; then
      echo "restore: the write-ahead log still holds data: $1-wal" >&2
      exit 1
    fi
    if [ -e "$1"-journal ]; then
      echo "restore: a rollback journal is present: $1-journal" >&2
      exit 1
    fi' sh "$LIVE_DB"
  LIVE_SEEN=$("${FINGERPRINT[@]}" "$LIVE_DB") || die "$?" "gate failed: the database being replaced could not be read"
  HINT=
fi

# --- 3. a staged copy of the snapshot ----------------------------------------------------

STAGE=$STATE/restore-staging-$STAMP
REPLACED=$STATE/replaced-$STAMP
STAGED_DB=$STAGE/questions.db

gate "create the staging directory" sudo install -d -o "$RUN_AS" -g "$RUN_GROUP" -m 0700 "$STAGE"
gate "copy the snapshot database to staging" \
  sudo install -o "$RUN_AS" -g "$RUN_GROUP" -m 0600 "$SNAPSHOT/questions.db" "$STAGED_DB"
say "gate: the staged database is in WAL mode"
MODE=$("${AS_SERVICE[@]}" sqlite3 "$STAGED_DB" 'PRAGMA journal_mode=WAL;') \
  || die "$?" "gate failed: the journal mode of the staged database could not be set"
if [ "$MODE" != wal ]; then
  die 1 "gate failed: the staged database is not in WAL mode ($MODE)"
fi
gate "create the staged upload and vault roots" \
  sudo install -d -o "$RUN_AS" -g "$RUN_GROUP" -m 0750 "$STAGE/uploads" "$STAGE/vault"
gate "extract the snapshot files to staging" "${AS_SERVICE[@]}" tar -C "$STAGE" -xzf "$SNAPSHOT/files.tar.gz"
FILES_STAGED=$("${FINGERPRINT[@]}" "$STAGE/uploads" "$STAGE/vault") \
  || die "$?" "gate failed: the extracted files could not be read"

# --- 4. schema: the copy is brought to the schema of this build ----------------------

MIGRATE=("${AS_SERVICE[@]}" "$PYTHON" -m src.manage_migrations --db "$STAGED_DB")
LIFECYCLE=("${AS_SERVICE[@]}" "$PYTHON" -m src.manage_lifecycle --db "$STAGED_DB")

HINT="A '(table-rebuild)' migration has its own procedure: docs/DEPLOYMENT.md, section 9."
gate "list the migrations pending on the copy" "${MIGRATE[@]}" --dry-run
gate "apply the pending migrations to the copy" "${MIGRATE[@]}" --apply
HINT=

# --- 4b. registration: pending credentials voided, registration held (D20 (a)) ------------
# On the copy, after its migration (which gives an old snapshot the registration tables) and
# before the checkpoint and fingerprint below, so that every byte the gate changes is in the
# bytes that are verified and compared. Nothing of the gate runs after that. The hold it sets
# is never cleared here; a hold the snapshot already carries is kept as it was.

REGISTRATIONS=("${AS_SERVICE[@]}" "$PYTHON" -m src.manage_accounts --db "$STAGED_DB")
HINT="Registration is held after every restore (docs/DEPLOYMENT.md, section 34); this step cannot be skipped."
gate "void pending registrations and hold registration on the copy (registrations-restore-gate)" \
  "${REGISTRATIONS[@]}" registrations-restore-gate
HINT=

# --- 5. lifecycle gates, all on the copy -----------------------------------------------
# Every lifecycle command refuses a database whose migrations differ from this
# build's, so each of them is also the check that step 4 left nothing pending.
# The order is hold, reconcile, clock: recovery classifies the deadlines and
# markers that reconciliation brings in, against everything the replaced
# database's clock had reached.

if [ "$REPLACED_UNREADABLE" = yes ]; then
  gate "hold every assignment (restore-hold; nothing can be carried over)" \
    "${LIFECYCLE[@]}" restore-hold "${AUDIT[@]}"
else
  HINT="If the database being replaced cannot be used at all, see --replaced-database-unreadable."
  gate "hold every assignment and carry over the holds of the database being replaced (restore-hold)" \
    "${LIFECYCLE[@]}" restore-hold --pre-restore-db "$LIVE_DB" "${AUDIT[@]}"
  HINT=
fi
gate "every assignment is held for the restore" "${LIFECYCLE[@]}" restore-status --expect-held all

if [ "$REPLACED_UNREADABLE" = yes ]; then
  say "no database to reconcile from: nothing is released and every assignment stays held"
else
  say "gate: reconcile from the database being replaced (reconcile)"
  HINT="A database that is the same as the snapshot, older than it, or another database is not evidence. Nothing is released from it."
  RECONCILED=0
  "${LIFECYCLE[@]}" reconcile --pre-restore-db "$LIVE_DB" --snapshot-db "$SNAPSHOT/questions.db" \
    --evidence-note "$EVIDENCE_NOTE" "${AUDIT[@]}" || RECONCILED=$?
  case $RECONCILED in
    0) ;;
    3) say "reconcile left assignments held (exit status 3); they are counted below" ;;
    *) die "$RECONCILED" "gate failed: reconcile from the database being replaced (exit status $RECONCILED)" ;;
  esac
  HINT=
fi

# The time is read now, when it is used, and by a person from another device: a
# reading passed in when the script was started would be as old as the copying.
say "gate: confirm the clock against a verified time (clock-recover)"
ATTEMPT=0
RECOVERED=1
while [ "$RECOVERED" -ne 0 ]; do
  ATTEMPT=$((ATTEMPT + 1))
  printf '%s' 'Current UTC time, read now from a device other than this server (YYYY-MM-DD HH:MM:SS): ' >&2
  VERIFIED_UTC=
  if ! IFS= read -r VERIFIED_UTC && [ -z "$VERIFIED_UTC" ]; then
    if [ "$ATTEMPT" -eq 1 ]; then
      die 2 "no verified time was given"
    fi
    die "$RECOVERED" "gate failed: confirm the clock (clock-recover, exit status $RECOVERED); no further reading was given"
  fi
  RECOVERED=0
  "${LIFECYCLE[@]}" "${RECOVER[@]}" --verified-utc "$VERIFIED_UTC" "${AUDIT[@]}" || RECOVERED=$?
  if [ "$RECOVERED" -ne 0 ]; then
    if [ "$ATTEMPT" -ge 3 ]; then
      HINT="Correct the server clock if it is wrong, then run the script again."
      die "$RECOVERED" "gate failed: confirm the clock (clock-recover, exit status $RECOVERED, $ATTEMPT readings)"
    fi
    say "clock-recover did not accept that reading (exit status $RECOVERED) and wrote nothing. Read the time again."
  fi
done

# After a checkpoint everything is in the main file, and from here on the copy is
# only read. Its bytes are remembered before it is verified, so that what is put
# in place below is what was verified.
gate "write the staged database out completely" \
  "${AS_SERVICE[@]}" sqlite3 "$STAGED_DB" 'PRAGMA wal_checkpoint(TRUNCATE);'
VERIFIED=$("${FINGERPRINT[@]}" "$STAGED_DB") || die "$?" "gate failed: the staged database could not be read"

gate "the copy verifies (verify)" "${LIFECYCLE[@]}" verify
gate "the copy has no pending registration and registration is held (registrations-restore-status)" \
  "${REGISTRATIONS[@]}" registrations-restore-status --expect held

say "gate: no assignment is left held for the restore, beyond the $ACCEPT_HELD acknowledged"
HELD=0
"${LIFECYCLE[@]}" restore-status --expect-held "$ACCEPT_HELD" || HELD=$?
if [ "$HELD" -ne 0 ]; then
  HINT="Nothing reopens a held assignment. To restore with exactly the listed assignments held and closed to everyone, run the script again with --accept-held and their number."
  die "$HELD" "gate failed: the assignments held for the restore are not the $ACCEPT_HELD acknowledged (exit status $HELD)"
fi

# --- 6. nothing has changed since it was examined ---------------------------------------------
# An empty write-ahead log and its index may be left next to a database that was
# only read (some SQLite builds keep them). Nothing has either database open, so
# the copy's are removed and the live ones are set aside below.

gate "the staged database is complete in its main file" sudo sh -c '
  set -eu
  if [ -s "$1"-wal ]; then
    echo "restore: the write-ahead log still holds data: $1-wal" >&2
    exit 1
  fi
  if [ -e "$1"-journal ]; then
    echo "restore: a rollback journal is present: $1-journal" >&2
    exit 1
  fi
  rm -f -- "$1"-wal "$1"-shm' sh "$STAGED_DB"
HINT="Something wrote to it while the restore was running. Nothing was replaced; find out what, then run the script again."
gate "the staged database is byte for byte the one that was verified" \
  "${FINGERPRINT[@]}" "$STAGED_DB" --expect "$VERIFIED"
gate "the staged uploads and vault are byte for byte what was extracted" \
  "${FINGERPRINT[@]}" "$STAGE/uploads" "$STAGE/vault" --expect "$FILES_STAGED"
if [ "$REPLACED_UNREADABLE" = no ]; then
  gate "the database being replaced is still complete in its main file" sudo sh -c '
    set -eu
    if [ -s "$1"-wal ] || [ -e "$1"-journal ]; then
      echo "restore: written to since it was examined: $1" >&2
      exit 1
    fi' sh "$LIVE_DB"
  gate "the database being replaced is byte for byte the one that was examined" \
    "${FINGERPRINT[@]}" "$LIVE_DB" --expect "$LIVE_SEEN"
fi
HINT=

# --- 7. replace the live state: uploads and vault first, the database last ---------------
# Each step is one rename on one filesystem. Whatever this run has moved it can
# move back, until the database is replaced. That last rename is the point of no
# return, and after it nothing is left to do but to check and to start.

gate "create the directory for the replaced state" \
  sudo install -d -o "$RUN_AS" -g "$RUN_GROUP" -m 0700 "$REPLACED"
gate "record in the lock that the replacement begins" sudo sh -c ': > "$1"' sh "$LOCK/swap-started"
PHASE=swap

for tree in uploads vault; do
  if sudo test -e "$STATE/$tree"; then
    UNDO+=("move|$STATE/$tree|$REPLACED/$tree")
    gate "set the live $tree aside" sudo sh -c "$MOVE" sh "$STATE/$tree" "$REPLACED/$tree"
  fi
  UNDO+=("move|$STAGE/$tree|$STATE/$tree")
  gate "put the restored $tree in place" sudo sh -c "$MOVE" sh "$STAGE/$tree" "$STATE/$tree"
done

if [ "$REPLACED_UNREADABLE" = no ]; then
  # The live database stays at its path until the rename that replaces it; a
  # second name for the same file keeps it afterwards.
  for sidecar in -wal -shm; do
    if sudo test -e "$LIVE_DB$sidecar"; then
      UNDO+=("move|$LIVE_DB$sidecar|$REPLACED/questions.db$sidecar")
      gate "set the empty questions.db$sidecar aside" \
        sudo sh -c "$MOVE" sh "$LIVE_DB$sidecar" "$REPLACED/questions.db$sidecar"
    fi
  done
  UNDO+=("link|$LIVE_DB|$REPLACED/questions.db")
  gate "keep the database being replaced" sudo ln -- "$LIVE_DB" "$REPLACED/questions.db"
  # The file that is about to lose its place, read under its second name: it is
  # the one reconciliation examined, to the byte, or it is not replaced.
  HINT="Something wrote to it while the restore was running. Find out what, then run the script again."
  gate "the database being set aside is byte for byte the one that was examined" \
    "${FINGERPRINT[@]}" "$REPLACED/questions.db" --expect "$LIVE_SEEN"
  HINT=
  # The one step that is not taken back. If the script is stopped at this line,
  # before or after the rename, what it does next is decided by what is at the
  # live path then (see finish), never by whether the next line was reached.
  gate "replace the live database by the copy" sudo mv -f -- "$STAGED_DB" "$LIVE_DB"
else
  # Not usable as evidence, and possibly not readable: set aside as it is, with
  # whatever belongs to it. Until the copy is in place there is no database at
  # the live path, and nothing can be started.
  for part in "" -wal -shm -journal; do
    if sudo test -e "$LIVE_DB$part"; then
      UNDO+=("move|$LIVE_DB$part|$REPLACED/questions.db$part")
      gate "set the unusable questions.db$part aside" \
        sudo sh -c "$MOVE" sh "$LIVE_DB$part" "$REPLACED/questions.db$part"
    fi
  done
  # As above: not taken back, and judged by what is at the live path.
  gate "put the copy in place as the live database" sudo sh -c "$MOVE" sh "$STAGED_DB" "$LIVE_DB"
fi
PHASE=committed
if ! sudo rmdir -- "$STAGE"; then
  say "note: the staging directory is not empty and was left in place: $STAGE"
fi

IN_PLACE=("${AS_SERVICE[@]}" "$PYTHON" -m src.manage_lifecycle --db "$LIVE_DB")
gate "the database now in place is byte for byte the one that was verified" \
  "${FINGERPRINT[@]}" "$LIVE_DB" --expect "$VERIFIED"
gate "the database now in place verifies (verify)" "${IN_PLACE[@]}" verify
gate "the database now in place has no pending registration and registration is held (registrations-restore-status)" \
  "${AS_SERVICE[@]}" "$PYTHON" -m src.manage_accounts --db "$LIVE_DB" registrations-restore-status --expect held
gate "the database now in place holds exactly what was acknowledged" \
  "${IN_PLACE[@]}" restore-status --expect-held "$ACCEPT_HELD"

# --- 8. unlock and start: the only start in this file, reached only through every gate above ---

say "every gate passed"
gate "release the restore lock" sudo rm -rf -- "$LOCK"
LOCK_HELD=no
START_ATTEMPTED=yes      # from here on, a script that does not end well stops the service again (finish)
if ! sudo systemctl start "$SERVICE"; then
  die 1 "the service did not start"
fi
if ! curl -fsS "$HEALTH_URL"; then
  die 1 "the service was started but its health check failed"
fi
PHASE=started

say "------------------------------------------------------------------"
say "The service is running on the restored state. What it replaced is in $REPLACED."
restart_timers
say "REGISTRATION IS HELD: pending registrations were voided and registration answers 'unavailable' until you run"
say "  manage_accounts registrations-release-hold --acknowledge-lost-history   (docs/DEPLOYMENT.md, section 34)."
say "Nothing releases it by itself. Sign-in is not affected."
if [ "$ACCEPT_HELD" -gt 0 ]; then
  say "RESTORED, NOT RECOVERED: $ACCEPT_HELD assignment(s) stay held and are closed to everyone."
  say "Nothing reopens them. See docs/DEPLOYMENT.md, section 26, 'Restore'."
else
  say "RESTORE COMPLETE: no assignment is held for the restore."
fi
say "That is all it says. Closed assignments are closed and deadlines are what they were; an"
say "assignment can still be held for a clock review (manage_lifecycle hold-list)."
exit 0
