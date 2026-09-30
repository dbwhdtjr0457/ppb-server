"""Opt-in integration with the matching, compiled macOS Swift rules evaluator."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import Header
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.auth_service import Principal, identity
from app.database import Base, get_db, make_engine
from app.game_api import get_rules
from app.game_service import initial_state
from app.main import app
from app.rules import SwiftRules


def rule_test_identity(x_ppb_account_id: str = Header(), x_ppb_device_id: str = Header()):
    return Principal(x_ppb_account_id, x_ppb_device_id, None)


pytestmark = pytest.mark.skipif(
    not os.getenv("PPB_TEST_RULES_EXECUTABLE"), reason="Set PPB_TEST_RULES_EXECUTABLE on macOS"
)


@pytest.fixture
def real_game(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/real.db")
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def database():
        with sessions() as session:
            yield session

    rules = SwiftRules(os.environ["PPB_TEST_RULES_EXECUTABLE"])
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_rules] = lambda: rules
    app.dependency_overrides[identity] = rule_test_identity
    with TestClient(app) as client:
        headers = {"X-PPB-Account-ID": str(uuid4()), "X-PPB-Device-ID": str(uuid4())}
        yield client, headers
    app.dependency_overrides.clear()
    engine.dispose()


def command(client, headers, payload):
    revision = client.get("/v1/state", headers=headers).json()["revision"]
    body = {"request_id": str(uuid4()), "expected_revision": revision, "command": payload}
    if payload["kind"] in {"buy_packs", "sell_spares", "sell_bulk", "pull_oripa", "refresh_oripa"}:
        body["price_version"] = client.get("/v1/prices", headers=headers).json()["version"]
        quote = client.post("/v1/quotes", headers=headers, json=body)
        assert quote.status_code == 200, quote.text
        body["quoted_tokens"] = quote.json()["tokens"]
    response = client.post("/v1/commands", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def test_real_purchase_bulk_open_replay_sale_and_oripa(real_game):
    client, headers = real_game
    command(client, headers, {"kind": "initialize"})
    command(client, headers, {"kind": "report_tokens", "collected_total": 10**12})
    reply, _ = command(client, headers, {"kind": "buy_packs", "set_id": "sv8pt5", "count": 1000})
    assert reply["snapshot"]["state"]["packs"]["sv8pt5"] == 1000
    # A completed request replays its original result even after its quote ages.
    stale_body = {
        "request_id": str(uuid4()),
        "expected_revision": reply["snapshot"]["revision"],
        "command": {"kind": "buy_packs", "set_id": "sv8pt5", "count": 1},
        "price_version": "obsolete",
        "quoted_tokens": 1,
    }
    rejected = client.post("/v1/commands", headers=headers, json=stale_body)
    assert rejected.status_code == 409 and "price_version_changed" in rejected.text
    stale_body["price_version"] = client.get("/v1/prices", headers=headers).json()["version"]
    rejected = client.post("/v1/commands", headers=headers, json=stale_body)
    assert rejected.status_code == 409 and "quote" in rejected.text
    reply, body = command(
        client, headers, {"kind": "open_packs", "set_id": "sv8pt5", "count": 1000}
    )
    packs = reply["result"]["packs"]["packs"]
    assert len(packs) == 1000
    assert all(pack["slotResults"] for pack in packs)
    state = reply["snapshot"]["state"]
    assert state["packs"].get("sv8pt5", 0) == 0
    assert state["packsOpened"] == 1000
    assert sum(state["cards"].values()) > 9000
    replay = client.post("/v1/commands", headers=headers, json=body).json()
    assert replay["replayed"] is True
    assert replay["result"] == reply["result"]
    assert replay["snapshot"]["state"]["packsOpened"] == 1000
    card = next(card for card, count in state["cards"].items() if count > 1)
    sold, _ = command(client, headers, {"kind": "sell_spares", "card_id": card, "count": 1})
    assert sold["snapshot"]["state"]["cards"][card] == state["cards"][card] - 1
    assert sold["snapshot"]["balance"] > reply["snapshot"]["balance"]
    bulk, _ = command(client, headers, {"kind": "sell_bulk", "card_ids": [card]})
    assert bulk["snapshot"]["state"]["cards"][card] == 1
    oripa, _ = command(client, headers, {"kind": "pull_oripa", "envelope": 0})
    assert oripa["result"]["oripa"]["card"]["id"] in oripa["snapshot"]["state"]["cards"]
    assert oripa["snapshot"]["balance"] < bulk["snapshot"]["balance"]
    command(
        client,
        headers,
        {"kind": "set_preferences", "opening_mode": "realistic", "favorite_card_id": card},
    )
    events = client.get("/v1/events", headers=headers).json()["events"]
    assert len(events) == 8
    assert all(event["rules_version"].startswith("ppb-server-v2/") for event in events)


def test_first_command_cannot_bypass_first_run_gift_eligibility(real_game):
    client, headers = real_game
    reply, _ = command(client, headers, {"kind": "report_tokens", "collected_total": 100000000})
    assert "v0.4.1-apology" in reply["snapshot"]["state"]["grantedGifts"]
    gift, _ = command(client, headers, {"kind": "claim_gift", "gift_id": "v0.4.1-apology"})
    assert gift["snapshot"]["balance"] == reply["snapshot"]["balance"]


def test_real_trade_market_reserved_npc_sale_and_statistics(real_game):
    client, first = real_game
    second = {"X-PPB-Account-ID": str(uuid4()), "X-PPB-Device-ID": str(uuid4())}

    def state(user):
        return client.get("/v1/state", headers=user).json()

    def online(user, route, action, **fields):
        body = {
            "request_id": str(uuid4()),
            "expected_revision": state(user)["revision"],
            "action": action,
            **fields,
        }
        reply = client.post("/v1/" + route, headers=user, json=body)
        assert reply.status_code == 200, reply.text
        return reply.json(), body

    for user in (first, second):
        command(client, user, {"kind": "initialize"})
        command(client, user, {"kind": "report_tokens", "collected_total": 10**10})
        command(client, user, {"kind": "buy_packs", "set_id": "sv8pt5", "count": 50})
        command(client, user, {"kind": "open_packs", "set_id": "sv8pt5", "count": 50})
    profiles = [
        client.get("/v1/profile", headers=user).json()["profile"] for user in (first, second)
    ]
    friend, _ = online(first, "friends", "friend_request", friend_code=profiles[1]["friend_code"])
    online(second, "friends", "friend_accept", target_id=friend["result"]["id"], target_version=0)
    a = next(key for key, amount in state(first)["state"]["printingCards"].items() if amount >= 4)
    b = next(
        key
        for key, amount in state(second)["state"]["printingCards"].items()
        if amount >= 3 and key != a
    )
    offer, _ = online(
        first,
        "trades",
        "trade_create",
        target_id=profiles[1]["public_id"],
        offered=[{"printing": a, "quantity": 2}],
        requested=[{"printing": b, "quantity": 1}],
    )
    # Existing bulk decomposition must leave reserved copies AND the final printing.
    command(client, first, {"kind": "sell_bulk", "card_ids": [a.rsplit("#", 1)[0]]})
    assert state(first)["state"]["printingCards"][a] == 3
    cards_before = sum(sum(state(user)["state"]["cards"].values()) for user in (first, second))
    accepted, body = online(
        second, "trades", "trade_accept", target_id=offer["result"]["id"], target_version=0
    )
    assert client.post("/v1/trades", headers=second, json=body).json()["replayed"]
    assert state(first)["state"]["printingCards"][a] == 1
    assert (
        sum(sum(state(user)["state"]["cards"].values()) for user in (first, second)) == cards_before
    )
    listing, _ = online(
        second, "market/listings", "listing_create", printing=a, quantity=1, unit_tokens=123
    )
    money = sum(state(user)["balance"] for user in (first, second))
    online(
        first,
        "market/listings",
        "listing_buy",
        target_id=listing["result"]["id"],
        target_version=0,
        quantity=1,
        unit_tokens=123,
    )
    assert sum(state(user)["balance"] for user in (first, second)) == money
    for user in (first, second):
        stats = client.get("/v1/stats", headers=user).json()
        assert stats["totals"]["opened"] == 50
        assert state(user)["state"]["packsOpened"] == 50


def test_real_rejection_leaves_state_unchanged(real_game):
    client, headers = real_game
    before = client.get("/v1/state", headers=headers).json()
    body = {
        "request_id": str(uuid4()),
        "expected_revision": 0,
        "command": {"kind": "open_packs", "set_id": "sv8pt5", "count": 1},
    }
    assert client.post("/v1/commands", headers=headers, json=body).status_code == 409
    assert client.get("/v1/state", headers=headers).json() == before


def test_token_report_keeps_existing_dex_bonus_and_bonus_windows():
    executable = Path(os.environ["PPB_TEST_RULES_EXECUTABLE"]).resolve()
    dex = json.loads((executable.parent / "PokePackBar_PokePackBar.bundle/dex.json").read_text())
    state = initial_state()
    state["claimedDex"] = [
        f"{entry['id']}#{len(entry['milestones']) - 1}" if entry["kind"] == "set" else entry["id"]
        for entry in dex["dexes"][:10]
    ]
    rules = SwiftRules(str(executable))
    credited, _, _ = rules.apply(state, {"kind": "apply_tokens", "collected_total": 100000})
    assert credited["usedSinceInstall"] == 100000
    assert credited["perkTokens"] > 0
    below = {
        "kind": "report_bonus",
        "windows": [
            {
                "key": "audit.weekly",
                "name": "audit",
                "kind": "weekly",
                "utilization": 50,
                "instance": "hash-of-window",
            }
        ],
    }
    seeded, _, _ = rules.apply(credited, below)
    below["windows"][0]["utilization"] = 100
    awarded, _, _ = rules.apply(seeded, below)
    repeated, _, _ = rules.apply(awarded, below)
    assert sum(awarded["packs"].values()) > 0
    assert repeated["packs"] == awarded["packs"]
