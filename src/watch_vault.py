"""
File watcher за Obsidian vault.

Observe-ва vault/ за .md промени и автоматично извиква sync_vault.py.
Дебаунсва бързи промени (ако чрез Cmd+S Obsidian запише няколко пъти за секунда,
синхронизираме само веднъж).

Pattern-ът:
  - Watch all .md files (create, modify, delete)
  - Skip _Templates/, _Attachments/, _backup_initial_mocs/
  - Debounce 3s — изчаква 3 сек тишина преди да sync-не
  - Лога всичко в data/watcher.log

Изисква: pip3 install watchdog

Употреба:
    python3 watch_vault.py [--vault PATH] [--db PATH] [--debounce SECONDS]

Spawn-ва се чрез launchd plist (виж install_watcher.sh).
За ръчно тестване: пусни в един терминал, в друг редактирай бележка.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("❌ Липсва пакет 'watchdog'. Инсталирай го с:\n   pip3 install watchdog")
    sys.exit(1)


# ============================================================
# Config
# ============================================================

IGNORED_PREFIXES = (
    "_Templates",
    "_Attachments",
    "_backup_initial_mocs",
    ".obsidian",
    ".git",
    ".trash",
)


# ============================================================
# Logging setup
# ============================================================

def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("watch_vault")
    logger.setLevel(logging.INFO)
    
    # File handler
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)
    
    # Stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(sh)
    
    return logger


# ============================================================
# Debounced sync trigger
# ============================================================

class DebouncedSync:
    """
    Trigger.notify() може да бъде извикан много пъти бързо.
    След последното извикване, изчаква `delay` сек и пуска sync_callback().
    """
    
    def __init__(self, delay: float, sync_callback):
        self.delay = delay
        self.sync_callback = sync_callback
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._pending_paths: set = set()
    
    def notify(self, path: str) -> None:
        with self._lock:
            self._pending_paths.add(path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()
    
    def _fire(self) -> None:
        with self._lock:
            paths = list(self._pending_paths)
            self._pending_paths.clear()
            self._timer = None
        try:
            self.sync_callback(paths)
        except Exception as e:
            # Don't let one failure kill the watcher
            logging.getLogger("watch_vault").exception(f"Sync callback failed: {e}")


# ============================================================
# Watchdog handler
# ============================================================

class VaultEventHandler(FileSystemEventHandler):
    def __init__(self, vault: Path, debouncer: DebouncedSync, logger):
        self.vault = vault
        self.debouncer = debouncer
        self.logger = logger
    
    def _should_ignore(self, path: str) -> bool:
        try:
            rel = Path(path).relative_to(self.vault)
        except ValueError:
            return True
        rel_str = str(rel)
        # Skip ignored top-level folders
        for prefix in IGNORED_PREFIXES:
            if rel_str.startswith(prefix):
                return True
        # Only care about .md files
        if not rel_str.endswith(".md"):
            return True
        return False
    
    def _handle(self, event_type: str, path: str) -> None:
        if self._should_ignore(path):
            return
        try:
            rel = Path(path).relative_to(self.vault)
        except ValueError:
            return
        self.logger.info(f"{event_type}: {rel}")
        self.debouncer.notify(str(rel))
    
    def on_created(self, event):
        if not event.is_directory:
            self._handle("created", event.src_path)
    
    def on_modified(self, event):
        if not event.is_directory:
            self._handle("modified", event.src_path)
    
    def on_deleted(self, event):
        if not event.is_directory:
            self._handle("deleted", event.src_path)
    
    def on_moved(self, event):
        if not event.is_directory:
            self._handle("moved-from", event.src_path)
            self._handle("moved-to", event.dest_path)


# ============================================================
# Sync runner
# ============================================================

def make_sync_runner(vault: Path, db: Path, sync_script: Path, logger):
    def run_sync(changed_paths: list) -> None:
        logger.info(f"⚡ Sync triggered ({len(changed_paths)} change(s))")
        cmd = [
            sys.executable,
            str(sync_script),
            "--vault", str(vault),
            "--db", str(db),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                # Parse last few stat lines from sync_vault output
                tail = "\n".join(result.stdout.strip().split("\n")[-10:])
                logger.info(f"✅ Sync OK\n{tail}")
            else:
                logger.error(f"❌ Sync failed (rc={result.returncode})")
                logger.error(f"stdout: {result.stdout}")
                logger.error(f"stderr: {result.stderr}")
        except subprocess.TimeoutExpired:
            logger.error("❌ Sync timeout (>120s)")
        except Exception as e:
            logger.exception(f"❌ Sync exception: {e}")
    return run_sync


# ============================================================
# Main
# ============================================================

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--vault", type=Path,
                   default=Path.home() / "dzi-generator" / "vault")
    p.add_argument("--db", type=Path,
                   default=Path.home() / "dzi-generator" / "data" / "questions.db")
    p.add_argument("--sync-script", type=Path,
                   default=Path.home() / "dzi-generator" / "src" / "sync_vault.py")
    p.add_argument("--debounce", type=float, default=3.0)
    p.add_argument("--log-file", type=Path,
                   default=Path.home() / "dzi-generator" / "data" / "watcher.log")
    args = p.parse_args()
    
    logger = setup_logging(args.log_file)
    
    if not args.vault.exists():
        logger.error(f"Vault не съществува: {args.vault}")
        sys.exit(1)
    if not args.db.exists():
        logger.error(f"DB не съществува: {args.db}")
        sys.exit(1)
    if not args.sync_script.exists():
        logger.error(f"sync_vault.py не съществува: {args.sync_script}")
        sys.exit(1)
    
    logger.info(f"🚀 Стартиране на watcher")
    logger.info(f"   Vault:       {args.vault}")
    logger.info(f"   DB:          {args.db}")
    logger.info(f"   Sync script: {args.sync_script}")
    logger.info(f"   Debounce:    {args.debounce}s")
    logger.info(f"   Log:         {args.log_file}")
    
    sync_runner = make_sync_runner(args.vault, args.db, args.sync_script, logger)
    debouncer = DebouncedSync(args.debounce, sync_runner)
    handler = VaultEventHandler(args.vault, debouncer, logger)
    
    observer = Observer()
    observer.schedule(handler, str(args.vault), recursive=True)
    observer.start()
    
    logger.info("👀 Наблюдавам (Ctrl+C за изход)")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("⏹️  Спирам watcher-а...")
        observer.stop()
    
    observer.join()
    logger.info("👋 Готово.")


if __name__ == "__main__":
    main()
