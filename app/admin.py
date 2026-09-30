"""Local operator tools only. There is deliberately no HTTP state-upload route."""

import argparse
import getpass
import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app import inventory
from app.config import settings
from app.database import SessionLocal
from app.game_service import balance, initial_state
from app.models import Account, GameEvent, Inventory
from app.rules import rules

LEGACY_SAVE_FORMAT = "legacy-local-v1"
BONUS_INSTANCE_FORMAT = "sha256-v1"


def snapshot_summary(state):
    return {
        "balance": balance(state),
        "card_kinds": len([count for count in state.get("cards", {}).values() if count > 0]),
        "cards": sum(state.get("cards", {}).values()),
        "printing_kinds": len(inventory.counts(state)),
        "printings": sum(inventory.counts(state).values()),
        "packs": sum(state.get("packs", {}).values()),
        "packs_opened": state.get("packsOpened", 0),
        "opening_history_entries": len(state.get("openingHistory", [])),
        "claimed_dex": len(state.get("claimedDex") or state.get("completedDex", [])),
        "coupons": len(state.get("coupons", [])),
        "bonus_instances": sum(
            len(values) for values in state.get("packGrantedInstances", {}).values()
        ),
    }


def normalize_legacy_bonus(state):
    """Only the explicit legacy import boundary hashes IDs, never normal inspect/reconcile."""
    grants = state.get("packGrantedInstances", {})
    if not isinstance(grants, dict) or any(
        not isinstance(values, list) or any(not isinstance(value, str) for value in values)
        for values in grants.values()
    ):
        raise ValueError("Invalid bonus instance history")
    state["packGrantedInstances"] = {
        key: [hashlib.sha256(value.encode()).hexdigest() if value else "" for value in values]
        for key, values in grants.items()
    }


def verify_import_preserves_resources(before, after):
    for key in (
        "usedSinceInstall",
        "spentTokens",
        "refundedTokens",
        "perkTokens",
        "marketEarnedTokens",
        "marketSpentTokens",
        "cards",
        "packs",
        "packsOpened",
        "cardsDisenchanted",
        "packPity",
        "coupons",
        "packGrantTier",
        "packGrantedInstances",
        "packGrantSeeded",
        "grantedGifts",
        "oripa",
        "openingHistory",
        "openingMode",
        "favoriteCardID",
        "title",
        "language",
    ):
        if key in before and before[key] != after.get(key):
            raise ValueError(f"Rules normalization changed protected save field: {key}")
    claimed = before.get("claimedDex") or before.get("completedDex", [])
    if claimed and claimed != after.get("claimedDex"):
        raise ValueError("Rules normalization changed claimed dex rewards")
    if any(
        after.get("printingCards", {}).get(key, 0) < count
        for key, count in before.get("printingCards", {}).items()
    ):
        raise ValueError("Rules normalization lost an existing printing")
    if not inventory.normalized(after):
        raise ValueError("Rules normalization did not preserve card/printing totals")


def import_save(account_id: str, source: Path, apply: bool, *, source_format: str):
    if source_format != LEGACY_SAVE_FORMAT:
        raise ValueError(
            "Only legacy-local-v1 source saves are supported; never import online caches"
        )
    account_id = str(UUID(account_id))
    data = source.read_bytes()
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Save exceeds 16 MiB")
    state = json.loads(data)
    if not isinstance(state, dict) or "usedSinceInstall" not in state or "cards" not in state:
        raise ValueError("Provide raw game-state.json, not an export envelope")
    source_digest = hashlib.sha256(data).hexdigest()
    canonical_digest = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    before = snapshot_summary(state)
    normalize_legacy_bonus(state)
    prepared = deepcopy(state)
    state, _, version = rules.apply(state, {"kind": "inspect"})
    verify_import_preserves_resources(prepared, state)
    amount = balance(state)
    report = {
        "applied": apply,
        "balance": amount,
        "card_kinds": len(state.get("cards", {})),
        "source_sha256": source_digest,
        "source_format": source_format,
        "bonus_instance_format": BONUS_INSTANCE_FORMAT,
        "rules_version": version,
        "before": before,
        "after": snapshot_summary(state),
        "cards_preserved": True,
        "changed_fields": sorted(
            key for key in prepared.keys() | state.keys() if prepared.get(key) != state.get(key)
        ),
        "historical_server_statistics_imported": False,
    }
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if db.get(Account, account_id) is not None:
            raise ValueError("Account already exists; import never merges or overwrites it")
        # The same write lock covers lookup and insert across processes. Include older imports,
        # whose only import marker was the original file fingerprint.
        previous = db.scalar(
            select(GameEvent.id).where(
                GameEvent.command == "operator_import",
                (GameEvent.fingerprint == source_digest)
                | (GameEvent.payload["canonical_sha256"].as_string() == canonical_digest),
            )
        )
        if previous is not None:
            raise ValueError("This save was already imported; a different UUID cannot duplicate it")
        if apply:
            account = Account(id=account_id, revision=1, balance=amount, state=state)
            db.add(account)
            db.flush()
            inventory.sync(db, account)
            db.add(
                GameEvent(
                    account_id=account_id,
                    device_id=str(UUID(int=0)),
                    request_id=str(uuid4()),
                    revision=1,
                    command="operator_import",
                    fingerprint=source_digest,
                    payload={
                        "sha256": source_digest,
                        "canonical_sha256": canonical_digest,
                        "source_format": source_format,
                        "bonus_instance_format": BONUS_INSTANCE_FORMAT,
                        "import_version": 1,
                    },
                    result=report,
                    balance_before=0,
                    balance_after=amount,
                    rules_version=version,
                )
            )
            db.commit()
        else:
            db.rollback()
    return report


def create_invite():
    """Create a fresh, unclaimed account and 10-minute signup code in one transaction."""
    from app.auth_service import new_link_code

    account_id = str(uuid4())
    state, _, _ = rules.apply(initial_state(), {"kind": "initialize"})
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        account = Account(id=account_id, revision=0, balance=balance(state), state=state)
        db.add(account)
        db.flush()
        inventory.sync(db, account)
        code = new_link_code(db, account_id)
        db.commit()
    return {"account_id": account_id, "link_code": code, "expires_in_seconds": 600}


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
    importer.add_argument("--source-format", required=True, choices=[LEGACY_SAVE_FORMAT])
    importer.add_argument("--apply", action="store_true", help="Default is validation/dry-run only")
    saver = sub.add_parser("backup")
    saver.add_argument("destination", type=Path)
    linker = sub.add_parser(
        "issue-link-code", help="One-time 10 minute code for an unlinked UUID account"
    )
    linker.add_argument("--account", required=True)
    sub.add_parser("create-invite", help="Create a new account and one-time signup code")
    reset = sub.add_parser("reset-password", help="Local operator reset; revokes every session")
    reset.add_argument("--email", required=True)
    projection = sub.add_parser(
        "reconcile", help="Audit projections; --apply rebuilds from native state/events"
    )
    projection.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action == "import-save":
        print(
            json.dumps(
                import_save(args.account, args.file, args.apply, source_format=args.source_format)
            )
        )
    elif args.action == "backup":
        backup(args.destination)
        print("Database backup completed (including committed WAL transactions).")
    elif args.action == "issue-link-code":
        from app.auth_service import issue_link_code

        with SessionLocal() as db:
            print(issue_link_code(db, args.account))
    elif args.action == "reconcile":
        print(json.dumps(reconcile(args.apply)))
    elif args.action == "create-invite":
        print(json.dumps(create_invite()))
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
