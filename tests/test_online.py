from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_auth import Rules, auth, headers, registered  # noqa: F401

from app.models import Account, CardTrade, MarketListing, Reservation, SocialProfile


@pytest.fixture
def online(auth, monkeypatch):  # noqa: F811
    client, sessions = auth
    original = Rules.apply

    def apply(self, state, command):
        if command["kind"] != "transfer":
            return original(self, state, command)
        state = deepcopy(state)
        for field, sign in [("remove", -1), ("add", 1)]:
            for key, number in command.get(field, {}).items():
                state["printingCards"][key] = state["printingCards"].get(key, 0) + sign * number
                card = key.split("#")[0]
                state["cards"][card] = state["cards"].get(card, 0) + sign * number
        state["marketEarnedTokens"] = state.get("marketEarnedTokens", 0) + command.get(
            "market_credit", 0
        )
        state["marketSpentTokens"] = state.get("marketSpentTokens", 0) + command.get(
            "market_debit", 0
        )
        return state, {}, "test-rules"

    monkeypatch.setattr(Rules, "apply", apply)
    monkeypatch.setattr(
        Rules,
        "catalogue",
        {
            "a-1": {"id": "a-1", "name": "Alpha", "name_ko": "알파", "tier": "R", "set_id": "a"},
            "a-2": {"id": "a-2", "name": "Beta", "name_ko": "베타", "tier": "R", "set_id": "a"},
        },
        raising=False,
    )
    a = registered(client, email="alpha@example.com")
    b = registered(client, email="beta@example.com")
    with sessions() as db:
        for user, key in [(a, "a-1"), (b, "a-2")]:
            account = db.get(Account, user["account_id"])
            account.state = {
                **account.state,
                "cards": {key: 5},
                "printingCards": {key + "#holo": 5},
                "usedSinceInstall": 10000,
            }
            account.balance = 10000
        db.commit()
    return client, sessions, a, b


def mutate(client, user, route, action, **fields):
    revision = client.get("/v1/state", headers=headers(user)).json()["revision"]
    body = {"request_id": str(uuid4()), "expected_revision": revision, "action": action, **fields}
    return client.post("/v1/" + route, headers=headers(user), json=body), body


def own(client, user):
    return client.get("/v1/profile", headers=headers(user)).json()["profile"]


def connect(client, a, b):
    first, second = own(client, a), own(client, b)
    reply, _ = mutate(client, a, "friends", "friend_request", friend_code=second["friend_code"])
    assert reply.status_code == 200, reply.text
    identifier = reply.json()["result"]["id"]
    accepted, _ = mutate(
        client, b, "friends", "friend_accept", target_id=identifier, target_version=0
    )
    assert accepted.status_code == 200, accepted.text
    return first, second


def test_settlement_failure_rolls_back_both_accounts_and_reservations(online, monkeypatch):
    from fastapi import HTTPException

    from app import commerce

    client, sessions, a, b = online
    _, second = connect(client, a, b)
    offer, _ = mutate(
        client,
        a,
        "trades",
        "trade_create",
        target_id=second["public_id"],
        offered=[{"printing": "a-1#holo", "quantity": 2}],
        requested=[{"printing": "a-2#holo", "quantity": 2}],
    )
    identifier = offer.json()["result"]["id"]
    before = [client.get("/v1/state", headers=headers(user)).json() for user in (a, b)]
    original = commerce.transfer
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise HTTPException(503, "injected_failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(commerce, "transfer", fail_second)
    reply, body = mutate(
        client, b, "trades", "trade_accept", target_id=identifier, target_version=0
    )
    assert reply.status_code == 503
    assert [client.get("/v1/state", headers=headers(user)).json() for user in (a, b)] == before
    with sessions() as db:
        assert db.get(CardTrade, identifier).status == "pending"
        assert db.scalar(select(Reservation).where(Reservation.owner == identifier)).quantity == 2
    monkeypatch.setattr(commerce, "transfer", original)
    assert client.post("/v1/trades", headers=headers(b), json=body).status_code == 200


def test_private_profile_friendship_and_blocking(online):
    client, _, a, b = online
    first, second = own(client, a), own(client, b)
    assert "@" not in str(first)
    assert client.get(f"/v1/friends/{second['public_id']}", headers=headers(a)).status_code == 404
    connect(client, a, b)
    shared = client.get(f"/v1/friends/{second['public_id']}", headers=headers(a)).json()
    assert "friend_code" not in shared and "account_id" not in shared
    assert shared["wishlist"] == [] and shared["binder"] == []
    assert (
        client.get(f"/v1/inventory?target={second['public_id']}", headers=headers(a)).status_code
        == 403
    )
    reply, _ = mutate(client, b, "profile", "profile", nickname="Beta", collection_public=True)
    assert reply.status_code == 200
    assert (
        client.get(f"/v1/inventory?target={second['public_id']}", headers=headers(a)).status_code
        == 200
    )
    reply, _ = mutate(client, a, "friends", "block", target_id=second["public_id"])
    assert reply.status_code == 200
    assert client.get(f"/v1/friends/{second['public_id']}", headers=headers(a)).status_code == 404
    assert (
        client.get(f"/v1/inventory?target={first['public_id']}", headers=headers(b)).status_code
        == 404
    )


def test_accept_cancel_race_never_duplicates_or_strands_escrow(online):
    client, sessions, a, b = online
    _, other = connect(client, a, b)
    created, _ = mutate(
        client,
        a,
        "trades",
        "trade_create",
        target_id=other["public_id"],
        offered=[{"printing": "a-1#holo", "quantity": 2}],
        requested=[{"printing": "a-2#holo", "quantity": 2}],
    )
    identifier = created.json()["result"]["id"]

    def settle(user, action):
        return mutate(client, user, "trades", action, target_id=identifier, target_version=0)[
            0
        ].status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(settle, a, "trade_cancel")
        two = pool.submit(settle, b, "trade_accept")
        assert sorted([one.result(), two.result()]) == [200, 409]
    with sessions() as db:
        assert db.get(CardTrade, identifier).status in {"accepted", "cancelled"}
        assert db.scalar(select(Reservation).where(Reservation.owner == identifier)) is None
        accounts = [db.get(Account, user["account_id"]) for user in (a, b)]
        assert sum(sum(row.state["cards"].values()) for row in accounts) == 10
        assert sum(row.balance for row in accounts) == 20000


def test_market_failure_restores_money_stock_and_reservation(online, monkeypatch):
    from fastapi import HTTPException

    from app import commerce

    client, sessions, a, b = online
    own(client, a)
    own(client, b)
    listing, _ = mutate(
        client,
        a,
        "market/listings",
        "listing_create",
        printing="a-1#holo",
        quantity=3,
        unit_tokens=123,
    )
    identifier = listing.json()["result"]["id"]
    original = commerce.transfer

    def fail_buyer(db, account, rules, **kwargs):
        if kwargs.get("debit"):
            raise HTTPException(503, "injected_failure")
        return original(db, account, rules, **kwargs)

    monkeypatch.setattr(commerce, "transfer", fail_buyer)
    response, body = mutate(
        client,
        b,
        "market/listings",
        "listing_buy",
        target_id=identifier,
        target_version=0,
        quantity=2,
        unit_tokens=123,
    )
    assert response.status_code == 503
    with sessions() as db:
        assert db.get(MarketListing, identifier).quantity == 3
        assert db.scalar(select(Reservation).where(Reservation.owner == identifier)).quantity == 3
        assert all(db.get(Account, user["account_id"]).balance == 10000 for user in (a, b))
    monkeypatch.setattr(commerce, "transfer", original)
    assert client.post("/v1/market/listings", headers=headers(b), json=body).status_code == 200


def test_large_market_pages_are_bounded_and_searchable(online):
    import time

    client, sessions, a, b = online
    own(client, a)
    with sessions() as db:
        db.add_all(
            [
                MarketListing(
                    id=str(uuid4()),
                    seller=a["account_id"],
                    printing="a-1#holo",
                    quantity=1,
                    unit_tokens=i + 1,
                    status="active",
                    version=0,
                    expires_at=int(time.time()) + 86400,
                    created_at=i,
                )
                for i in range(5000)
            ]
        )
        db.commit()
    started = time.perf_counter()
    response = client.get(
        "/v1/market/listings?q=알파&sort=price&limit=50&offset=4950", headers=headers(b)
    )
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    page = response.json()
    assert len(page["items"]) == 50 and page["next_offset"] is None
    assert page["items"][0]["unit_tokens"] == 4951
    assert elapsed < 5
    print(f"5000 listing final-page search: {elapsed:.3f}s, 50 returned")


def test_profile_retry_wishlist_matches_and_binder(online):
    client, sessions, a, b = online
    connect(client, a, b)
    for user, wished in [(a, "a-2"), (b, "a-1")]:
        reply, _ = mutate(
            client, user, "profile", "profile", nickname="trainer", wishlist_public=True
        )
        assert reply.status_code == 200
        reply, _ = mutate(
            client, user, "wishlist", "wishlist", wishes=[{"card_id": wished, "target": 1}]
        )
        assert reply.status_code == 200
    assert len(client.get("/v1/matches", headers=headers(a)).json()["items"]) == 1
    reply, body = mutate(client, a, "binder", "binder", binder=["a-1#holo"])
    assert reply.status_code == 200, reply.text
    replay = client.post("/v1/binder", headers=headers(a), json=body)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert own(client, a)["binder"] == ["a-1#holo"]
    invalid, _ = mutate(client, a, "binder", "binder", binder=["a-2#holo"])
    assert invalid.status_code == 409
    wrong, _ = mutate(client, a, "binder", "binder", binder=["a-1#holo"] * 37)
    assert wrong.status_code == 422
    with sessions() as db:
        assert db.scalar(select(SocialProfile).where(SocialProfile.account_id == a["account_id"]))


def test_reservation_preserves_one_copy_and_no_private_notifications(online):
    from app import inventory

    client, sessions, a, b = online
    own(client, a)
    with sessions() as db:
        account = db.get(Account, a["account_id"])
        inventory.reserve(db, account, "test-owner", {"a-1#holo": 4})
        db.commit()
        assert inventory.available(db, account)["a-1#holo"] == 0
        assert inventory.floors(db, account.id)["a-1#holo"] == 5
    rows = client.get("/v1/inventory", headers=headers(a)).json()["items"]
    assert rows[0]["quantity"] == 5 and rows[0]["available"] == 0 and rows[0]["reserved"] == 4
    assert client.get("/v1/notifications", headers=headers(b)).json()["items"] == []
    with sessions() as db:
        assert db.scalar(select(Reservation)).quantity == 4


def test_atomic_trade_replay_final_copy_and_expiry(online):
    from app.maintenance import expire

    client, sessions, a, b = online
    _, second = connect(client, a, b)
    offer, _ = mutate(
        client,
        a,
        "trades",
        "trade_create",
        target_id=second["public_id"],
        offered=[{"printing": "a-1#holo", "quantity": 4}],
        requested=[{"printing": "a-2#holo", "quantity": 2}],
    )
    assert offer.status_code == 200, offer.text
    identifier = offer.json()["result"]["id"]
    excess, _ = mutate(
        client,
        a,
        "trades",
        "trade_create",
        target_id=second["public_id"],
        offered=[{"printing": "a-1#holo", "quantity": 1}],
        requested=[{"printing": "a-2#holo", "quantity": 1}],
    )
    assert excess.status_code == 409
    accepted, body = mutate(
        client, b, "trades", "trade_accept", target_id=identifier, target_version=0
    )
    assert accepted.status_code == 200, accepted.text
    assert client.post("/v1/trades", headers=headers(b), json=body).json()["replayed"]
    left = client.get("/v1/state", headers=headers(a)).json()["state"]
    right = client.get("/v1/state", headers=headers(b)).json()["state"]
    assert left["printingCards"] == {"a-1#holo": 1, "a-2#holo": 2}
    assert right["printingCards"] == {"a-1#holo": 4, "a-2#holo": 3}
    offer, _ = mutate(
        client,
        b,
        "trades",
        "trade_create",
        target_id=own(client, a)["public_id"],
        offered=[{"printing": "a-1#holo", "quantity": 1}],
        requested=[{"printing": "a-2#holo", "quantity": 1}],
    )
    assert offer.status_code == 200
    with sessions() as db:
        row = db.get(CardTrade, offer.json()["result"]["id"])
        row.expires_at = 0
        db.commit()
        expire(db)
        assert db.get(CardTrade, row.id).status == "expired"
        assert list(db.scalars(select(Reservation))) == []


def test_market_partial_purchase_conservation_retry_and_self_rejection(online):
    client, sessions, a, b = online
    own(client, a)
    own(client, b)
    listed, _ = mutate(
        client,
        a,
        "market/listings",
        "listing_create",
        printing="a-1#holo",
        quantity=4,
        unit_tokens=300,
    )
    assert listed.status_code == 200, listed.text
    identifier = listed.json()["result"]["id"]
    myself, _ = mutate(
        client,
        a,
        "market/listings",
        "listing_buy",
        target_id=identifier,
        target_version=0,
        quantity=1,
        unit_tokens=300,
    )
    assert myself.status_code == 403
    purchased, body = mutate(
        client,
        b,
        "market/listings",
        "listing_buy",
        target_id=identifier,
        target_version=0,
        quantity=2,
        unit_tokens=300,
    )
    assert purchased.status_code == 200, purchased.text
    assert client.post("/v1/market/listings", headers=headers(b), json=body).json()["replayed"]
    with sessions() as db:
        left, right = db.get(Account, a["account_id"]), db.get(Account, b["account_id"])
        assert left.balance == 10600 and right.balance == 9400
        assert left.state.get("refundedTokens", 0) == 0
        assert left.state["marketEarnedTokens"] == right.state["marketSpentTokens"] == 600
        assert db.get(MarketListing, identifier).quantity == 2
        assert db.get(Reservation, (left.id, "a-1#holo", identifier)).quantity == 2
    cancelled, _ = mutate(
        client, a, "market/listings", "listing_cancel", target_id=identifier, target_version=1
    )
    assert cancelled.status_code == 200
    assert client.get("/v1/market/listings?q=알파", headers=headers(b)).json()["items"] == []


def test_concurrent_market_purchase_only_one_wins(online):
    client, sessions, a, b = online
    own(client, a)
    own(client, b)
    c = registered(client, email="gamma@example.com")
    with sessions() as db:
        account = db.get(Account, c["account_id"])
        account.state = {**account.state, "usedSinceInstall": 10000}
        account.balance = 10000
        db.commit()
    own(client, c)
    listed, _ = mutate(
        client,
        a,
        "market/listings",
        "listing_create",
        printing="a-1#holo",
        quantity=1,
        unit_tokens=300,
    )
    identifier = listed.json()["result"]["id"]

    def buy(user):
        return mutate(
            client,
            user,
            "market/listings",
            "listing_buy",
            target_id=identifier,
            target_version=0,
            quantity=1,
            unit_tokens=300,
        )[0].status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(buy, [b, c])) == [200, 409]
    with sessions() as db:
        assert sum(account.balance for account in db.scalars(select(Account))) == 30000
        assert db.get(MarketListing, identifier).status == "sold"


def test_block_cancels_reserved_trade_and_prevents_market(online):
    client, sessions, a, b = online
    _, second = connect(client, a, b)
    offer, _ = mutate(
        client,
        a,
        "trades",
        "trade_create",
        target_id=second["public_id"],
        offered=[{"printing": "a-1#holo", "quantity": 1}],
        requested=[{"printing": "a-2#holo", "quantity": 1}],
    )
    blocked, _ = mutate(client, a, "friends", "block", target_id=second["public_id"])
    assert blocked.status_code == 200
    with sessions() as db:
        assert db.get(CardTrade, offer.json()["result"]["id"]).status == "cancelled"
        assert list(db.scalars(select(Reservation))) == []
