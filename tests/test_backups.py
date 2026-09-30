import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.backups import verified_backup
from app.database import Base, make_engine
from app.models import ServerJob


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.sqlite3"
    url = f"sqlite:///{path}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260930_0006')"))
    yield url, tmp_path / "backups"
    engine.dispose()


def test_verified_backup_preserves_wal_and_prunes_only_owned_files(source):
    url, directory = source
    directory.mkdir()
    unrelated = directory / "manual.sqlite3"
    unrelated.write_text("keep me")
    with sqlite3.connect(url.removeprefix("sqlite:///")) as db:
        db.execute("INSERT INTO items (name) VALUES ('committed in WAL')")
        db.commit()
        first = verified_backup(url, directory, 2)
        second = verified_backup(url, directory, 2)
        third = verified_backup(url, directory, 2)
        for path in directory.glob("ppb-*.sqlite3"):
            assert path.stat().st_mode & 0o777 == 0o600
            with sqlite3.connect(path) as copy:
                assert copy.execute("SELECT name FROM items").fetchone()[0] == "committed in WAL"
    assert unrelated.read_text() == "keep me"
    assert third.exists()
    assert len(list(directory.glob("ppb-*.sqlite3"))) == 2
    assert first.exists() or second.exists()


def test_failed_validation_keeps_last_good_backups(source, monkeypatch):
    url, directory = source
    saved = verified_backup(url, directory)

    def fail(_):
        raise ValueError("invalid")

    monkeypatch.setattr("app.backups.validate", fail)
    with pytest.raises(ValueError):
        verified_backup(url, directory, 1)
    assert list(directory.iterdir()) == [saved]


def test_symlink_directory_rejected(source, tmp_path):
    url, directory = source
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory)
    with pytest.raises(ValueError):
        verified_backup(url, link)


def test_backup_job_lease_failure_retry_and_success(source, monkeypatch):
    from types import SimpleNamespace

    from app import jobs

    url, directory = source
    engine = make_engine(url)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionLocal", sessions)
    monkeypatch.setattr(
        jobs,
        "settings",
        SimpleNamespace(database_url=url, backup_directory=str(directory), backup_keep=2),
    )
    with sessions() as db:
        assert jobs.claim(db, "backup", 100, 600)
    with sessions() as db:
        assert jobs.claim(db, "backup", 101, 600) is None
    jobs.backup_database()
    with sessions() as db:
        row = db.get(ServerJob, "backup")
        assert row.last_success and row.error is None and row.owner is None
        row.next_run = 0
        db.commit()

    def fail(*_):
        raise OSError("private path must not appear")

    monkeypatch.setattr("app.backups.verified_backup", fail)
    jobs.backup_database()
    with sessions() as db:
        row = db.get(ServerJob, "backup")
        assert row.error and "private path" not in row.error
        assert row.owner is None and row.next_run > row.last_success
    assert len(list(directory.glob("ppb-*.sqlite3"))) == 1
    engine.dispose()


def test_empty_expiry_does_not_take_writer_lock(source):
    from sqlalchemy import event

    from app.maintenance import expire

    url, _ = source
    engine = make_engine(url)
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, *args: statements.append(statement),
    )
    with sessionmaker(bind=engine)() as db:
        expire(db)
    assert not any("BEGIN IMMEDIATE" in sql for sql in statements)
    engine.dispose()
