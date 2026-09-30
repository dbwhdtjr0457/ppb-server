import os
import sqlite3
import subprocess
import sys


def test_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / "migration.db"
    environment = {**os.environ, "PPB_DATABASE_URL": f"sqlite:///{path}"}
    for action, target in [("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")]:
        subprocess.run(
            [sys.executable, "-m", "alembic", action, target],
            env=environment,
            check=True,
            capture_output=True,
        )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("select version_num from alembic_version").fetchone()[0]
            == "20260930_0006"
        )
        assert connection.execute("select count(*) from accounts").fetchone()[0] == 0


def test_auth_migration_preserves_legacy_wallet(tmp_path):
    path = tmp_path / "legacy.db"
    environment = {**os.environ, "PPB_DATABASE_URL": f"sqlite:///{path}"}

    def migrate(target):
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", target],
            env=environment,
            check=True,
            capture_output=True,
        )

    migrate("20260930_0002")
    with sqlite3.connect(path) as connection:
        columns = connection.execute("PRAGMA table_info(accounts)").fetchall()
        assert "state" in [column[1] for column in columns]
        connection.execute(
            "INSERT INTO accounts (id, revision, balance, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "00000000-0000-0000-0000-000000000001",
                7,
                900,
                '{"cards":{"base1-4":3},"usedSinceInstall":900}',
                "2026-09-30",
                "2026-09-30",
            ),
        )
        before = connection.execute("SELECT * FROM accounts").fetchall()
    migrate("head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM accounts").fetchall() == before
        assert connection.execute("SELECT count(*) FROM password_identities").fetchone()[0] == 0
