"""All account writes, deduplication and event records commit together.

SQLite has no SELECT FOR UPDATE. BEGIN IMMEDIATE serializes writers BEFORE
reading a revision; an in-process lock would fail with multiple workers.
"""

import hashlib
import json
import time
from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app import inventory, prices, statistics
from app.game_schemas import CommandRequest
from app.models import Account, Device, GameEvent, TokenPolicy, utcnow
from app.performance import record

MAX_COUNTER = 10**15


def initial_state():
    return {
        "schemaVersion": 2,
        "usedSinceInstall": 0,
        "spentTokens": 0,
        "refundedTokens": 0,
        "perkTokens": 0,
        "packs": {},
        "cards": {},
        "printingCards": {},
        "openingMode": "game",
    }


def balance(state: dict) -> int:
    values = [
        state.get(k, 0) for k in ("usedSinceInstall", "spentTokens", "refundedTokens", "perkTokens")
    ]
    values.extend([state.get("marketEarnedTokens", 0), state.get("marketSpentTokens", 0)])
    if any(type(v) is not int or not 0 <= v <= MAX_COUNTER for v in values):
        raise ValueError("Invalid wallet counters")
    result = values[0] - values[1] + values[2] + values[3] + values[4] - values[5]
    if not 0 <= result <= MAX_COUNTER:
        raise ValueError("Invalid wallet balance")
    for key in ("packs", "cards", "printingCards"):
        counts = state.get(key, {})
        if not isinstance(counts, dict) or any(
            type(v) is not int or not 0 <= v <= MAX_COUNTER for v in counts.values()
        ):
            raise ValueError("Invalid inventory")
    return result


def snapshot(account: Account, db=None):
    state = deepcopy(account.state)
    # The remaining prize multiset is public, but the unopened envelope-to-card
    # mapping must stay server-side. Preserve opened positions for existing UI.
    box = state.get("oripa")
    if box:
        opened = set(box["opened"])
        hidden = sorted(box["cards"][i] for i in range(len(box["cards"])) if i not in opened)
        positions = (i for i in range(len(box["cards"])) if i not in opened)
        for i, card in zip(positions, hidden, strict=True):
            box["cards"][i] = card
    reply = {
        "account_id": account.id,
        "revision": account.revision,
        "balance": account.balance,
        "state": state,
    }
    if db is not None:
        reply["reserved"] = inventory.reserved(db, account.id)
        reply["available_printings"] = inventory.available(db, account)
    return reply


def read_state(db: Session, account_id: str):
    account = db.get(Account, account_id)
    # Reading does not silently create an account or award a gift.
    if account is None:
        return {"account_id": account_id, "revision": 0, "balance": 0, "state": initial_state()}
    return snapshot(account, db)


def execute(
    db: Session,
    account_id: str,
    device_id: str,
    request: CommandRequest,
    rules,
    session_hash: str | None = None,
    after_apply=None,
    scope: str | None = None,
):
    payload = request.command.model_dump(mode="json")
    # Include the device and revision: one idempotency key is one exact request.
    canonical = json.dumps(
        {
            "device": device_id,
            "request": request.model_dump(
                mode="json",
                exclude={
                    key
                    for key in ("price_version", "quoted_tokens")
                    if getattr(request, key) is None
                },
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if scope is not None:
        canonical = json.dumps({"request": canonical, "scope": scope}, sort_keys=True)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        lock_started = time.monotonic()
        db.execute(text("BEGIN IMMEDIATE"))
        record("write_lock", time.monotonic() - lock_started)
        if session_hash is not None:
            from app.auth_service import Principal, require_session

            require_session(db, Principal(account_id, device_id, session_hash))
        existing = db.scalar(
            select(GameEvent).where(
                GameEvent.account_id == account_id, GameEvent.request_id == str(request.request_id)
            )
        )
        if existing:
            if existing.fingerprint != fingerprint:
                raise HTTPException(409, "idempotency_key_reused")
            current = db.get(Account, account_id)
            response = {
                "snapshot": snapshot(current, db),
                "result": existing.result,
                "event_revision": existing.revision,
                "replayed": True,
            }
            db.rollback()
            return response
        account = db.get(Account, account_id)
        if account is None:
            if request.expected_revision != 0:
                raise HTTPException(409, {"code": "revision_conflict", "revision": 0})
            # First-run gift eligibility is a server invariant, not a promise
            # that clients will send initialize before earning/spending tokens.
            initialized, _, _ = rules.apply(initial_state(), {"kind": "initialize"})
            account = Account(
                id=account_id, revision=0, balance=balance(initialized), state=initialized
            )
            db.add(account)
            db.flush()
        if account.revision != request.expected_revision:
            raise HTTPException(409, {"code": "revision_conflict", "revision": account.revision})
        state = deepcopy(account.state)
        price_version, price_payload = prices.current(db, rules)
        protected = inventory.floors(db, account_id)
        prices.check_version(payload["kind"], request.price_version, price_version)
        if price_version and payload["kind"] in prices.PRICED_COMMANDS:
            _, quote, _ = prices.apply(
                rules,
                state,
                {**payload, "kind": "quote", "quote_kind": payload["kind"]},
                price_payload,
                protected,
            )
            if request.quoted_tokens != quote.get("tokens"):
                raise HTTPException(409, "quote_changed")
        device = db.get(Device, (account_id, device_id))
        if device is None:
            device = Device(
                account_id=account_id, id=device_id, collected_total=0, policy_version=0
            )
            db.add(device)
        device.seen_at = utcnow()
        before = account.balance
        if payload["kind"] == "report_tokens":
            total = payload["collected_total"]
            delta = max(0, total - device.collected_total)
            device.collected_total = max(total, device.collected_total)
            policy = db.get(TokenPolicy, account_id)
            credit_status = "credited"
            if policy and device.policy_version != policy.version:
                delta, credit_status = 0, "policy_baseline"
                device.policy_version = policy.version
            if policy and policy.collector_device_id not in (None, device_id):
                delta, credit_status = 0, "other_collector_device"
            state, result, version = prices.apply(
                rules,
                state,
                {"kind": "apply_tokens", "collected_total": delta},
                price_payload,
                protected,
            )
            result["credited"] = delta
            result["credit_status"] = credit_status
        else:
            state, result, version = prices.apply(rules, state, payload, price_payload, protected)
        if any(
            state.get("printingCards", {}).get(key, 0) < floor for key, floor in protected.items()
        ):
            raise HTTPException(409, "reserved_printing_protected")
        result["opening_mode"] = account.state.get("openingMode", "game")
        result["price_version"] = price_version
        if request.rules_version is not None and request.rules_version != version:
            raise HTTPException(409, "rules_version_mismatch")
        after = balance(state)
        account.state = state
        if inventory.normalized(state):
            inventory.sync(db, account)
        account.balance = after
        account.revision += 1
        account.updated_at = utcnow()
        event = GameEvent(
            account_id=account_id,
            device_id=device_id,
            request_id=str(request.request_id),
            revision=account.revision,
            command=payload["kind"],
            fingerprint=fingerprint,
            payload=payload,
            result=result,
            balance_before=before,
            balance_after=after,
            rules_version=version,
        )
        db.add(event)
        db.flush()
        statistics.project(db, event, result["opening_mode"])
        if after_apply is not None:
            after_apply(db, account, result)
            # JSON columns need reassignment after the callback adds the durable job receipt.
            event.result = dict(result)
            flag_modified(event, "result")
        response = {
            "snapshot": snapshot(account, db),
            "result": result,
            "event_revision": account.revision,
            "replayed": False,
        }
        db.commit()
        return response
    except OperationalError as error:
        db.rollback()
        raise HTTPException(503, "database_unavailable_retry_same_request") from error
    except ValueError as error:
        db.rollback()
        raise HTTPException(409, "invalid_resource_state") from error
    except BaseException:
        db.rollback()
        raise
