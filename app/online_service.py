import hashlib
import json
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app import inventory, social
from app.auth_service import require_session
from app.game_service import balance, snapshot
from app.models import Account, GameEvent, OnlineReceipt, utcnow


def prepare(db, account, rules):
    if not inventory.normalized(account.state):
        account.state, _, _ = rules.apply(account.state, {"kind": "inspect"})
    inventory.sync(db, account)


def journal(db, account, who, command, result, before, version):
    account.balance = balance(account.state)
    account.revision += 1
    account.updated_at = utcnow()
    request_id = (
        str(command.request_id)
        if who.account_id == account.id
        else str(uuid5(NAMESPACE_URL, f"{who.account_id}/{command.request_id}/{account.id}"))
    )
    # No friend code, nickname, email or private profile payload in resource logs.
    db.add(
        GameEvent(
            account_id=account.id,
            device_id=who.device_id,
            request_id=request_id,
            revision=account.revision,
            command=command.action,
            fingerprint=version,
            payload={"transaction_id": str(command.request_id), "action": command.action},
            result=result,
            balance_before=before,
            balance_after=account.balance,
            rules_version="online-v1",
        )
    )


def execute(db, who, command, rules):
    canonical = json.dumps(
        {"device": who.device_id, "command": command.model_dump(mode="json")}, sort_keys=True
    )
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        if who.session_hash:
            require_session(db, who)
        actor = db.get(Account, who.account_id)
        if actor is None:
            raise HTTPException(404, "account_not_found")
        receipt = db.get(OnlineReceipt, (actor.id, str(command.request_id)))
        if receipt:
            if receipt.fingerprint != fingerprint:
                raise HTTPException(409, "idempotency_key_reused")
            response = {"result": receipt.result, "snapshot": snapshot(actor, db), "replayed": True}
            db.rollback()
            return response
        if actor.revision != command.expected_revision:
            raise HTTPException(409, "revision_conflict")
        prepare(db, actor, rules)
        before = actor.balance
        # Retain pre-transaction balances for both participants' journals.
        balances = dict(db.execute(select(Account.id, Account.balance)).all())
        if command.action.startswith("trade_"):
            from app.commerce import trade

            result, affected = trade(db, actor, command, rules)
        elif command.action.startswith("listing_"):
            from app.commerce import market

            result, affected = market(db, actor, command, rules)
        else:
            result, affected = social.mutate(db, actor, command, rules)
        for account_id in sorted(affected):
            account = db.get(Account, account_id)
            prepare(db, account, rules)
            journal(
                db,
                account,
                who,
                command,
                result,
                before if account_id == actor.id else balances[account_id],
                fingerprint,
            )
        db.add(
            OnlineReceipt(
                account_id=actor.id,
                request_id=str(command.request_id),
                fingerprint=fingerprint,
                result=deepcopy(result),
            )
        )
        db.flush()
        response = {"result": result, "snapshot": snapshot(actor, db), "replayed": False}
        db.commit()
        return response
    except OperationalError as error:
        db.rollback()
        raise HTTPException(503, "retry_same_request") from error
    except BaseException:
        db.rollback()
        raise
