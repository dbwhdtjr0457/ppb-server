import os
import sqlite3
import subprocess
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


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
            == "20261015_0009"
        )
        assert connection.execute("select count(*) from accounts").fetchone()[0] == 0
        profiles = {row[1]: row for row in connection.execute("PRAGMA table_info(social_profiles)")}
        # Existing profiles share their spare cards with friends unless they opt out.
        assert profiles["trade_list_public"][3] == 1 and profiles["trade_list_public"][4] in {
            "1",
            "'1'",
            "TRUE",
            "true",
        }
        trades = [row[1] for row in connection.execute("PRAGMA table_info(card_trades)")]
        assert "counter_of" in trades
        listings = {row[1]: row for row in connection.execute("PRAGMA table_info(market_listings)")}
        # The server's rotation listings have no seller and say where they came from.
        assert listings["seller"][3] == 0 and "source" in listings
        purchases = [row[1] for row in connection.execute("PRAGMA table_info(market_purchases)")]
        assert purchases == ["listing_id", "account_id", "created_at"]
        # Trades carry money on one side; existing trades start at zero.
        trades = {row[1]: row for row in connection.execute("PRAGMA table_info(card_trades)")}
        assert trades["offered_tokens"][3] == 1 and trades["requested_tokens"][3] == 1
        offers = [row[1] for row in connection.execute("PRAGMA table_info(market_offers)")]
        assert offers[:4] == ["id", "listing_id", "buyer", "quantity"]

    # A fully migrated database must pass the actual readiness handler, not only
    # schema inspection. Otherwise a new migration can leave /ready pinned behind.
    from app.main import ready

    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        assert ready(session)["status"] == "ready"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE alembic_version SET version_num='20261014_0008'")
    with Session(engine) as session, pytest.raises(HTTPException) as error:
        ready(session)
    assert error.value.status_code == 503
    engine.dispose()


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


def test_rotation_migration_keeps_listings_and_their_checks(tmp_path):
    """SQLite rebuilds market_listings to drop NOT NULL; nothing else may change."""
    path = tmp_path / "listings.db"
    environment = {**os.environ, "PPB_DATABASE_URL": f"sqlite:///{path}"}

    def migrate(target):
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", target],
            env=environment,
            check=True,
            capture_output=True,
        )

    migrate("20261008_0007")
    seller = "00000000-0000-0000-0000-000000000001"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO accounts (id, revision, balance, state, created_at, updated_at) "
            "VALUES (?, 0, 0, '{}', '2026-10-14', '2026-10-14')",
            (seller,),
        )
        connection.execute(
            "INSERT INTO market_listings VALUES "
            "('l1', ?, 'sv1-1#normal', 2, 500, 'active', 0, 9, 1)",
            (seller,),
        )
    migrate("head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id, seller, source FROM market_listings").fetchall() == [
            ("l1", seller, None)
        ]
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='market_listings'"
            )
        }
        assert {
            "ix_market_listings_seller",
            "ix_market_listings_printing",
            "ix_market_listings_expiry",
        } <= indexes
        for broken in ("quantity = -1", "unit_tokens = 0"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE market_listings SET {broken}")
