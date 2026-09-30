import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import admin
from app.database import Base, make_engine
from app.game_service import initial_state
from app.models import Account, GameEvent, Inventory
from app.rules import SwiftRules


class InspectRules:
    def apply(self, state, command):
        assert command == {"kind": "inspect"}
        state = deepcopy(state)
        if not state.get("printingCards"):
            state["printingCards"] = {
                key + "#normal": count for key, count in state.get("cards", {}).items()
            }
        return state, {}, "test-import"


@pytest.fixture
def importer(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/source.db")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(admin, "SessionLocal", sessions)
    monkeypatch.setattr(admin, "rules", InspectRules())
    yield sessions
    engine.dispose()


def test_import_is_dry_run_and_never_overwrites(tmp_path, importer):
    source = tmp_path / "save.json"
    source.write_text(json.dumps(initial_state()))
    account = str(uuid4())
    options = {"source_format": admin.LEGACY_SAVE_FORMAT}
    assert admin.import_save(account, source, False, **options)["applied"] is False
    with importer() as db:
        assert db.scalar(select(func.count()).select_from(Account)) == 0
    assert admin.import_save(account, source, True, **options)["applied"] is True
    with importer() as db:
        assert db.get(Account, account).revision == 1
        assert db.scalar(select(func.count()).select_from(GameEvent)) == 1
    with pytest.raises(ValueError, match="already exists"):
        admin.import_save(account, source, True, **options)


def test_import_converts_bonus_once_projects_cards_and_rejects_duplicate_snapshot(
    tmp_path, importer
):
    state = {
        **initial_state(),
        "usedSinceInstall": 900,
        "spentTokens": 120,
        "cards": {"base1-4": 3},
        "packs": {"base1": 2},
        "packGrantSeeded": True,
        "packGrantTier": {"codex-weekly": 1},
        "packGrantedInstances": {"codex-weekly": ["local-account:window-123"]},
    }
    source = tmp_path / "save.json"
    original = json.dumps(state).encode()
    source.write_bytes(original)
    account = str(uuid4())
    report = admin.import_save(account, source, True, source_format=admin.LEGACY_SAVE_FORMAT)
    assert report["before"]["cards"] == report["after"]["cards"] == 3
    assert report["before"]["balance"] == report["after"]["balance"] == 780
    assert report["after"]["printings"] == 3
    assert source.read_bytes() == original
    with importer() as db:
        saved = db.get(Account, account)
        hashed = hashlib.sha256(b"local-account:window-123").hexdigest()
        assert saved.state["packGrantedInstances"] == {"codex-weekly": [hashed]}
        assert db.get(Inventory, (account, "base1-4#normal")).quantity == 3
        event = db.scalar(select(GameEvent))
        assert event.payload["source_format"] == admin.LEGACY_SAVE_FORMAT
        assert event.payload["bonus_instance_format"] == admin.BONUS_INSTANCE_FORMAT
        assert event.payload["import_version"] == 1
    # Changing whitespace or object key order cannot bypass the import receipt.
    for encoded in (original, json.dumps(state, sort_keys=True, indent=2).encode()):
        source.write_bytes(encoded)
        with pytest.raises(ValueError, match="already imported"):
            admin.import_save(str(uuid4()), source, True, source_format=admin.LEGACY_SAVE_FORMAT)
    with importer() as db:
        assert db.scalar(select(func.count()).select_from(Account)) == 1


def test_import_refuses_resource_loss_before_writing(tmp_path, importer, monkeypatch):
    class LosingRules(InspectRules):
        def apply(self, state, command):
            result, _, version = super().apply(state, command)
            result["cards"] = {}
            result["printingCards"] = {}
            return result, {}, version

    monkeypatch.setattr(admin, "rules", LosingRules())
    source = tmp_path / "save.json"
    source.write_text(json.dumps({**initial_state(), "cards": {"base1-4": 3}}))
    with pytest.raises(ValueError, match="protected save field: cards"):
        admin.import_save(str(uuid4()), source, True, source_format=admin.LEGACY_SAVE_FORMAT)
    with importer() as db:
        assert db.scalar(select(func.count()).select_from(Account)) == 0


def test_import_requires_explicit_legacy_format(tmp_path, importer):
    with pytest.raises(ValueError, match="never import online caches"):
        admin.import_save(str(uuid4()), tmp_path / "unused.json", False, source_format="online")


def test_simultaneous_imports_of_one_snapshot_only_create_one_account(tmp_path, importer):
    source = tmp_path / "save.json"
    source.write_text(json.dumps(initial_state()))

    def run(_):
        try:
            admin.import_save(str(uuid4()), source, True, source_format=admin.LEGACY_SAVE_FORMAT)
            return "created"
        except ValueError as error:
            assert "already imported" in str(error)
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(run, range(2))) == ["created", "duplicate"]
    with importer() as db:
        assert db.scalar(select(func.count()).select_from(Account)) == 1


@pytest.mark.parametrize(
    "backend", ["python"] + (["swift"] if os.getenv("PPB_TEST_RULES_EXECUTABLE") else [])
)
def test_real_import_does_not_award_the_same_bonus_window_again(
    tmp_path, importer, monkeypatch, backend
):
    from app.native_rules import PythonRules

    rules = (
        PythonRules()
        if backend == "python"
        else SwiftRules(os.environ["PPB_TEST_RULES_EXECUTABLE"])
    )
    monkeypatch.setattr(admin, "rules", rules)
    raw_instance = "local-account:window-123"
    state = {
        **initial_state(),
        "cards": {"base1-4": 3},
        "packs": {"base1": 2},
        "packGrantSeeded": True,
        "packGrantTier": {"codex-weekly": 1},
        "packGrantedInstances": {"codex-weekly": [raw_instance]},
    }
    source = tmp_path / "save.json"
    source.write_text(json.dumps(state))
    account_id = str(uuid4())
    admin.import_save(account_id, source, True, source_format=admin.LEGACY_SAVE_FORMAT)
    with importer() as db:
        imported = db.get(Account, account_id).state
    window = {
        "key": "codex-weekly",
        "name": "Codex weekly",
        "kind": "weekly",
        "utilization": 100,
        "instance": hashlib.sha256(raw_instance.encode()).hexdigest(),
    }
    unchanged, _, _ = rules.apply(imported, {"kind": "report_bonus", "windows": [window]})
    assert unchanged["packs"] == imported["packs"]
    assert unchanged["packGrantedInstances"] == imported["packGrantedInstances"]
    # A genuinely new window still earns the normal reward after migration.
    window["instance"] = hashlib.sha256(b"local-account:window-456").hexdigest()
    rewarded, _, _ = rules.apply(unchanged, {"kind": "report_bonus", "windows": [window]})
    assert sum(rewarded["packs"].values()) > sum(unchanged["packs"].values())


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
