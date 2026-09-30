"""Local operator tools only. There is deliberately no HTTP state-upload route."""

import argparse
import getpass
import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import SessionLocal
from app.game_service import balance
from app.models import Account, GameEvent, Inventory
from app.rules import rules


def import_save(account_id: str, source: Path, apply: bool):
    account_id = str(UUID(account_id))
    data = source.read_bytes()
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Save exceeds 16 MiB")
    state = json.loads(data)
    if not isinstance(state, dict) or "usedSinceInstall" not in state or "cards" not in state:
        raise ValueError("Provide raw game-state.json, not an export envelope")
    state, _, version = rules.apply(state, {"kind": "inspect"})
    amount = balance(state)
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if db.get(Account, account_id) is not None:
            raise ValueError("Account already exists; import never merges or overwrites it")
        if apply:
            db.add(Account(id=account_id, revision=1, balance=amount, state=state))
            db.flush()
            db.add(
                GameEvent(
                    account_id=account_id,
                    device_id=str(UUID(int=0)),
                    request_id=str(uuid4()),
                    revision=1,
                    command="operator_import",
                    fingerprint=hashlib.sha256(data).hexdigest(),
                    payload={"sha256": hashlib.sha256(data).hexdigest()},
                    result={},
                    balance_before=0,
                    balance_after=amount,
                    rules_version=version,
                )
            )
            db.commit()
        else:
            db.rollback()
    return {"applied": apply, "balance": amount, "card_kinds": len(state.get("cards", {}))}


def backup(destination: Path):
    source = make_url(settings.database_url).database
    if not source or not Path(source).is_file():
        raise ValueError("A file-backed SQLite database is required")
    # O_EXCL prevents accidental replacement of any earlier backup.
    with destination.open("xb"):
        pass
    destination.chmod(0o600)
    with sqlite3.connect(f"file:{Path(source).resolve()}?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as target:
            src.backup(target)


def reconcile(apply: bool):
    """Audit native inventory projection and rebuild missing statistics idempotently."""
    from app import inventory, statistics

    output = []
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        for account in db.scalars(select(Account)):
            state, _, _ = rules.apply(account.state, {"kind": "inspect"})
            projected = dict(
                db.execute(
                    select(Inventory.printing, Inventory.quantity).where(
                        Inventory.account_id == account.id
                    )
                ).all()
            )
            expected = inventory.counts(state)
            held = inventory.reserved(db, account.id)
            valid = all(expected.get(key, 0) >= amount + 1 for key, amount in held.items())
            output.append(
                {
                    "account": account.id,
                    "inventory_matches": projected == expected,
                    "reservations_valid": valid,
                }
            )
            if not valid:
                raise ValueError(
                    "Reservation mismatch; no changes applied. "
                    "Restore from backup or investigate logs."
                )
            if apply:
                account.state = state
                inventory.sync(db, account)
                statistics.rebuild(db, account.id)
        if apply:
            db.commit()
        else:
            db.rollback()
    return {"applied": apply, "accounts": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    importer = sub.add_parser("import-save")
    importer.add_argument("--account", required=True)
    importer.add_argument("--file", type=Path, required=True)
    importer.add_argument("--apply", action="store_true", help="Default is validation/dry-run only")
    saver = sub.add_parser("backup")
    saver.add_argument("destination", type=Path)
    linker = sub.add_parser(
        "issue-link-code", help="One-time 10 minute code for an unlinked UUID account"
    )
    linker.add_argument("--account", required=True)
    reset = sub.add_parser("reset-password", help="Local operator reset; revokes every session")
    reset.add_argument("--email", required=True)
    projection = sub.add_parser(
        "reconcile", help="Audit projections; --apply rebuilds from native state/events"
    )
    projection.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action == "import-save":
        print(json.dumps(import_save(args.account, args.file, args.apply)))
    elif args.action == "backup":
        backup(args.destination)
        print("Database backup completed (including committed WAL transactions).")
    elif args.action == "issue-link-code":
        from app.auth_service import issue_link_code

        with SessionLocal() as db:
            print(issue_link_code(db, args.account))
    elif args.action == "reconcile":
        print(json.dumps(reconcile(args.apply)))
    else:
        from app.auth_service import reset_password

        password = getpass.getpass("New password (8–128 characters): ")
        if password != getpass.getpass("Confirm password: "):
            raise ValueError("Passwords did not match")
        with SessionLocal() as db:
            reset_password(db, args.email, password)
        print("Password changed; all sessions revoked.")


if __name__ == "__main__":
    main()
