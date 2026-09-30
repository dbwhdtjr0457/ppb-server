"""Consistent SQLite snapshots with restore rehearsal; never restore live data automatically."""

import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import make_url

BACKUP_NAME = re.compile(r"ppb-\d{10}-[0-9a-f]{32}\.sqlite3")


def validate(connection):
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("Backup integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Backup foreign-key check failed")
    for table in ("accounts", "game_events", "reservations", "online_receipts", "server_jobs"):
        connection.execute(f"SELECT 1 FROM {table} LIMIT 1")
    return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def verified_backup(database_url, directory: Path, keep=14):
    source = make_url(database_url).database
    if not source or not Path(source).is_file():
        raise ValueError("A file-backed SQLite database is required")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("Backup directory must not be a symlink")
    name = f"ppb-{int(time.time()):010d}-{uuid4().hex}.sqlite3"
    destination = directory / name
    started = time.monotonic()

    def progress(*_):
        if time.monotonic() - started > 300:
            raise TimeoutError("Backup time budget exceeded")

    # Same filesystem: publish only after integrity and a separate restore succeed.
    with tempfile.TemporaryDirectory(prefix=".ppb-backup-", dir=directory) as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite3"
        with sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True) as src:
            with sqlite3.connect(snapshot) as target:
                target.set_progress_handler(lambda: int(time.monotonic() - started > 300), 10000)
                src.backup(target, pages=256, progress=progress, sleep=0.01)
                revision = validate(target)
                with sqlite3.connect(Path(temporary) / "restore.sqlite3") as restored:
                    restored.set_progress_handler(
                        lambda: int(time.monotonic() - started > 300), 10000
                    )
                    target.backup(restored, pages=256, progress=progress)
                    if validate(restored) != revision:
                        raise ValueError("Restore rehearsal revision mismatch")
        snapshot.chmod(0o600)
        with snapshot.open("rb") as handle:
            os.fsync(handle.fileno())
        os.rename(snapshot, destination)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    # Only our own recognized regular backups, and only AFTER successful publication.
    backups = sorted(
        (
            p
            for p in directory.iterdir()
            if BACKUP_NAME.fullmatch(p.name)
            and p.is_file()
            and not p.is_symlink()
            and p != destination
        ),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    for old in backups[max(1, keep) - 1 :]:
        old.unlink()
    return destination
