"""All account writes, deduplication and event records commit together.

Rules are pure: compute outside SQLite's global write lock, then atomically
recheck every mutable input under BEGIN IMMEDIATE before publishing a result.
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
from app.models import Account, Device, GameEvent, Inventory, TokenPolicy, utcnow
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


def public_state(state: dict) -> dict:
    """The account state as clients see it (a copy)."""
    state = deepcopy(state)
    # The remaining prize multiset is public, but the unopened envelope-to-card
    # mapping must stay server-side. Preserve opened positions for existing UI.
    box = state.get("oripa")
    if box:
        opened = set(box["opened"])
        hidden = sorted(box["cards"][i] for i in range(len(box["cards"])) if i not in opened)
        positions = (i for i in range(len(box["cards"])) if i not in opened)
        for i, card in zip(positions, hidden, strict=True):
            box["cards"][i] = card
    return state


def state_digest(view: dict) -> str:
    """Identifies a public state exactly. Clients compare it before applying a patch, which
    also catches internal repairs that change state without advancing the revision."""
    canonical = json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def snapshot(account: Account, db=None):
    state = public_state(account.state)
    reply = {
        "account_id": account.id,
        "revision": account.revision,
        "balance": account.balance,
        "state": state,
        "state_digest": state_digest(state),
    }
    if db is not None:
        reply["reserved"] = inventory.reserved(db, account.id)
        reply["available_printings"] = inventory.available(db, account)
    return reply


def history_patch(old: list, new: list):
    """The capped opening history only drops its oldest records and appends new ones.

    Returns {"drop", "append"} or None when the change is not of that shape.
    """
    if not new:
        return None
    start = next((i for i, entry in enumerate(old) if entry.get("id") == new[0].get("id")), None)
    if start is None:
        return {"drop": len(old), "append": new}
    kept = len(old) - start
    if old[start:] != new[:kept]:
        return None
    return {"drop": start, "append": new[kept:]}


def state_patch(before: dict, after: dict) -> dict:
    """Top-level replacements, removals and an append/drop patch for the opening history.

    A long-time player's state is about 1.3 MB and 97% of it is the 1,000-pack opening
    history, yet a token report changes a few counters. Sending only what changed keeps
    command replies small over a slow uplink.
    """
    patch = {"set": {}, "merge": {}, "remove": sorted(key for key in before if key not in after)}
    for key, value in after.items():
        if key == "openingHistory" or (key in before and before[key] == value):
            continue
        old_value = before.get(key)
        if isinstance(old_value, dict) and isinstance(value, dict):
            # Card counts and first-seen dates have thousands of entries; one pack
            # changes a handful.
            patch["merge"][key] = {
                "set": {k: v for k, v in value.items() if k not in old_value or old_value[k] != v},
                "remove": sorted(k for k in old_value if k not in value),
            }
        else:
            patch["set"][key] = value
    old, new = before.get("openingHistory"), after.get("openingHistory")
    if old != new:
        history = history_patch(old or [], new or []) if new is not None else None
        if history is None:
            if new is None:
                patch["remove"].append("openingHistory")
            else:
                patch["set"]["openingHistory"] = new
        else:
            patch["history"] = history
    return patch


def snapshot_patch(account: Account, db, before_state: dict, base_revision: int):
    before = public_state(before_state)
    after = public_state(account.state)
    return {
        "account_id": account.id,
        "revision": account.revision,
        "balance": account.balance,
        "base_revision": base_revision,
        "base_digest": state_digest(before),
        "state_digest": state_digest(after),
        "reserved": inventory.reserved(db, account.id),
        # available_printings (about 2,000 entries for a long-time player) is not read by the
        # app, so patches leave it out; full snapshots keep it for compatibility.
        **state_patch(before, after),
    }


def read_state(db: Session, account_id: str):
    account = db.get(Account, account_id)
    # Reading does not silently create an account or award a gift.
    if account is None:
        return {"account_id": account_id, "revision": 0, "balance": 0, "state": initial_state()}
    return snapshot(account, db)


def _replay(db, account_id, request, fingerprint):
    existing = db.scalar(
        select(GameEvent).where(
            GameEvent.account_id == account_id, GameEvent.request_id == str(request.request_id)
        )
    )
    if existing is None:
        return None
    if existing.fingerprint != fingerprint:
        raise HTTPException(409, "idempotency_key_reused")
    return {
        "snapshot": snapshot(db.get(Account, account_id), db),
        "result": existing.result,
        "event_revision": existing.revision,
        "replayed": True,
    }


def _credit_inputs(db, account_id, device_id):
    device = db.get(Device, (account_id, device_id))
    policy = db.get(TokenPolicy, account_id)
    return (
        (device.collected_total, device.policy_version) if device else None,
        (policy.version, policy.collector_device_id) if policy else None,
    )


def _sync_inventory(db, account, previous_printings):
    if not inventory.normalized(account.state):
        return
    current_printings = inventory.counts(account.state)
    if previous_printings == current_printings:
        # Token reports, purchases and preferences usually leave printings alone.
        # Verify the real projection with scalar columns instead of constructing
        # hundreds of ORM objects. Missing/stale legacy projections still repair.
        projected = dict(
            db.execute(
                select(Inventory.printing, Inventory.quantity).where(
                    Inventory.account_id == account.id
                )
            ).all()
        )
        if projected == current_printings:
            return
    inventory.sync(db, account)


def execute(
    db: Session,
    account_id: str,
    device_id: str,
    request: CommandRequest,
    rules,
    session_hash: str | None = None,
    after_apply=None,
    scope: str | None = None,
    patch: bool = False,
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
    transaction_started = None
    try:
        prepare_started = time.monotonic()
        # An explicit deferred transaction gives all preparation reads one WAL
        # snapshot without preventing another account from committing a write.
        db.execute(text("BEGIN"))
        if session_hash is not None:
            from app.auth_service import Principal, require_session

            require_session(db, Principal(account_id, device_id, session_hash))
        response = _replay(db, account_id, request, fingerprint)
        if response is not None:
            db.rollback()
            return response
        account = db.get(Account, account_id)
        existed = account is not None
        revision = account.revision if existed else 0
        if revision != request.expected_revision:
            raise HTTPException(409, {"code": "revision_conflict", "revision": revision})
        observed_state = account.state if existed else None
        state = deepcopy(observed_state)
        previous_printings = (
            inventory.counts(state) if existed and inventory.normalized(state) else None
        )
        before = account.balance if existed else None
        price_version, price_payload = prices.current(db, rules)
        protected = inventory.floors(db, account_id)
        credit_inputs = _credit_inputs(db, account_id, device_id)
        prices.check_version(payload["kind"], request.price_version, price_version)
        db.rollback()
        record("command_prepare", time.monotonic() - prepare_started)

        compute_started = time.monotonic()
        try:
            if not existed:
                # First-run gift remains an invariant even if initialize was omitted.
                state, _, _ = rules.apply(initial_state(), {"kind": "initialize"})
                before = balance(state)
            opening_mode = state.get("openingMode", "game")
            device_total, device_policy = credit_inputs[0] or (0, 0)
            policy = credit_inputs[1]
            priced = price_version and payload["kind"] in prices.PRICED_COMMANDS
            combined = getattr(rules, "apply_with_quote", None) if priced else None
            if combined is not None:
                state, result, version, quoted_tokens = combined(
                    state, payload, prices=price_payload, protected=protected
                )
                if request.quoted_tokens != quoted_tokens:
                    raise HTTPException(409, "quote_changed")
            else:
                if priced:
                    _, quote, _ = prices.apply(
                        rules,
                        state,
                        {**payload, "kind": "quote", "quote_kind": payload["kind"]},
                        price_payload,
                        protected,
                    )
                    if request.quoted_tokens != quote.get("tokens"):
                        raise HTTPException(409, "quote_changed")
                if payload["kind"] == "report_tokens":
                    total = payload["collected_total"]
                    delta = max(0, total - device_total)
                    device_total = max(total, device_total)
                    credit_status = "credited"
                    if policy and device_policy != policy[0]:
                        delta, credit_status = 0, "policy_baseline"
                        device_policy = policy[0]
                    if policy and policy[1] not in (None, device_id):
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
                    state, result, version = prices.apply(
                        rules, state, payload, price_payload, protected
                    )
        finally:
            record("rules_compute", time.monotonic() - compute_started)
        if any(
            state.get("printingCards", {}).get(key, 0) < floor for key, floor in protected.items()
        ):
            raise HTTPException(409, "reserved_printing_protected")
        result["opening_mode"] = opening_mode
        result["price_version"] = price_version
        if request.rules_version is not None and request.rules_version != version:
            raise HTTPException(409, "rules_version_mismatch")
        after = balance(state)

        lock_started = time.monotonic()
        db.execute(text("BEGIN IMMEDIATE"))
        transaction_started = time.monotonic()
        record("write_lock", transaction_started - lock_started)
        if session_hash is not None:
            require_session(db, Principal(account_id, device_id, session_hash))
        # Another worker may have committed this exact request while we computed.
        response = _replay(db, account_id, request, fingerprint)
        if response is not None:
            db.rollback()
            return response
        account = db.get(Account, account_id)
        current_revision = account.revision if account is not None else 0
        if (
            current_revision != revision
            or (account is not None) != existed
            # Internal legacy normalization/reconciliation may not advance a
            # public revision. Do not overwrite those concurrent state repairs.
            or (existed and (account.state != observed_state or account.balance != before))
        ):
            raise HTTPException(409, {"code": "revision_conflict", "revision": current_revision})
        if _credit_inputs(db, account_id, device_id) != credit_inputs:
            raise HTTPException(409, "token_policy_changed")
        if prices.current_version(db, rules) != price_version:
            raise HTTPException(409, "price_version_changed")
        if inventory.floors(db, account_id) != protected:
            raise HTTPException(409, "reserved_printings_changed")
        if account is None:
            account = Account(id=account_id, revision=0, balance=before, state=state)
            db.add(account)
            db.flush()
        device = db.get(Device, (account_id, device_id))
        if device is None:
            device = Device(account_id=account_id, id=device_id)
            db.add(device)
        device.collected_total, device.policy_version = device_total, device_policy
        device.seen_at = utcnow()
        account.state = state
        _sync_inventory(db, account, previous_printings)
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
        response = {"result": result, "event_revision": account.revision, "replayed": False}
        if patch and existed:
            # Clients that understand patches apply this to the state they hold at
            # base_revision and fall back to GET /v1/state when the base digest differs.
            response["snapshot_patch"] = snapshot_patch(account, db, observed_state, revision)
        else:
            response["snapshot"] = snapshot(account, db)
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
    finally:
        if transaction_started is not None:
            record("write_transaction", time.monotonic() - transaction_started)
