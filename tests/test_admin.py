import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import admin
from app.database import Base, make_engine
from app.game_service import initial_state
from app.models import Account, GameEvent


class InspectRules:
    def apply(self, state, command):
        assert command == {"kind": "inspect"}
        return state, {}, "test-import"


def test_import_is_dry_run_and_never_overwrites(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/source.db")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(admin, "SessionLocal", sessions)
    monkeypatch.setattr(admin, "rules", InspectRules())
    source = tmp_path / "save.json"
    source.write_text(json.dumps(initial_state()))
    account = str(uuid4())
    assert admin.import_save(account, source, False)["applied"] is False
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Account)) == 0
    assert admin.import_save(account, source, True)["applied"] is True
    with sessions() as db:
        assert db.get(Account, account).revision == 1
        assert db.scalar(select(func.count()).select_from(GameEvent)) == 1
    with pytest.raises(ValueError, match="already exists"):
        admin.import_save(account, source, True)
    engine.dispose()


def test_backup_includes_wal_and_refuses_overwrite(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.sqlite3"
    monkeypatch.setattr(admin, "settings", SimpleNamespace(database_url=f"sqlite:///{source}"))
    with sqlite3.connect(source) as connection:
        connection.execute("pragma journal_mode=WAL")
        connection.execute("create table sample (value integer)")
        connection.execute("insert into sample values (42)")
        connection.commit()
        admin.backup(destination)
    with sqlite3.connect(destination) as restored:
        assert restored.execute("select value from sample").fetchone()[0] == 42
    with pytest.raises(FileExistsError):
        admin.backup(destination)
