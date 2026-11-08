"""Prepare the single mounted data directory, drop root, then exec the server.

No recursive ownership changes or database rewrites happen here. Existing
volume files with incompatible ownership require an explicit operator repair.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_UID = 10001
APP_GID = 10001
DATA_DIR = Path("/data")


def main() -> None:
    token = os.environ.get("TRADINGBOT_DASHBOARD_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("TRADINGBOT_DASHBOARD_TOKEN must be configured with at least 32 characters")
    command = sys.argv[1:]
    if not command:
        raise SystemExit("No server command supplied")
    db_path = Path(os.environ.get("TRADINGBOT_DB", "/data/tradingbot.sqlite3"))
    if not db_path.is_absolute() or db_path.parent != DATA_DIR or db_path.name in ("", ".", ".."):
        raise SystemExit("Container TRADINGBOT_DB must name a database file directly inside /data")
    os.umask(0o077)
    if DATA_DIR.is_symlink():
        raise SystemExit("/data must be a mounted directory, not a symbolic link")
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.geteuid() == 0:
        # A fresh Fly Volume is root-owned. Touch only its mount directory;
        # never recursively chown existing user files or a database backup.
        os.chown(DATA_DIR, APP_UID, APP_GID)
        os.chmod(DATA_DIR, 0o700)
        os.setgroups([])
        os.setgid(APP_GID)
        os.setuid(APP_UID)
    if os.geteuid() == 0:
        raise SystemExit("Refusing to run the trading server as root")
    if not os.access(DATA_DIR, os.R_OK | os.W_OK | os.X_OK):
        raise SystemExit("Data directory is not writable by the application user")
    if db_path.exists() and not os.access(db_path, os.R_OK | os.W_OK):
        raise SystemExit("Existing database ownership prevents application access; repair that file explicitly")
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
