"""The daily rotation: cards the server lists on the online market itself."""

import logging
import time

import pytest
from sqlalchemy import select
from test_auth import Rules, auth, headers, registered  # noqa: F401
from test_online import mutate, online  # noqa: F401

from app import maintenance, rotation
from app.models import Account, GameEvent, MarketListing, MarketPurchase, Notification
from app.observability import logger

OFFERS = [("a-1#holo", 1200), ("a-2#holo", 900)]


@pytest.fixture
def market(online, monkeypatch):  # noqa: F811
    monkeypatch.setattr(Rules, "rotation_offers", lambda self, rng, prices=None: OFFERS, False)
    return online


def listings(client, user):
    reply = client.get("/v1/market/listings", headers=headers(user))
    assert reply.status_code == 200, reply.text
    return [item for item in reply.json()["items"] if item.get("rotation")]


def buy(client, user, item, **changes):
    fields = {
        "target_id": item["id"],
        "target_version": item["version"],
        "quantity": 1,
        "unit_tokens": item["unit_tokens"],
        **changes,
    }
    reply, _ = mutate(client, user, "market/listings", "listing_buy", **fields)
    return reply


def test_opening_the_market_puts_up_one_lineup_a_day(market):
    client, sessions, a, b = market
    shown = listings(client, a)
    assert sorted((item["printing"], item["unit_tokens"]) for item in shown) == sorted(OFFERS)
    for item in shown:
        assert item["quantity"] == rotation.STOCK
        assert item["bought"] is False and item["mine"] is False
        # No seller to show or befriend; the app names the rotation itself.
        assert item["nickname"] == "" and item["public_id"] == ""
    assert len(listings(client, b)) == 2
    with sessions() as db:
        assert len(db.scalars(select(MarketListing)).all()) == 2
        assert rotation.restock(db, Rules()) is False


def test_next_day_takes_yesterdays_lineup_down_quietly(market):
    client, sessions, a, _ = market
    listings(client, a)
    tomorrow = rotation.round_start(time.time()) + rotation.DAY + 60
    with sessions() as db:
        assert rotation.restock(db, Rules(), now=tomorrow) is True
        rows = db.scalars(select(MarketListing)).all()
        assert sorted(row.status for row in rows) == ["active", "active", "expired", "expired"]
        assert all(
            row.expires_at == rotation.round_start(tomorrow) + rotation.DAY
            for row in rows
            if row.status == "active"
        )
        assert db.scalars(select(Notification)).all() == []


def test_each_account_buys_one_copy_and_pays_nobody(market):
    client, sessions, a, b = market
    item = next(i for i in listings(client, a) if i["printing"] == "a-1#holo")
    assert buy(client, a, item).status_code == 200
    again = buy(client, a, item, target_version=item["version"] + 1)
    assert again.status_code == 409 and again.json()["detail"] == "rotation_limit_reached"
    # A copy bought by someone else since b loaded the market does not turn b away.
    assert buy(client, b, item).status_code == 200
    two = buy(
        client, b, next(i for i in listings(client, b) if i["printing"] == "a-2#holo"), quantity=2
    )
    assert two.status_code == 409 and two.json()["detail"] == "rotation_limit_reached"
    wrong = buy(client, b, {**item, "unit_tokens": 1})
    assert wrong.status_code == 409 and wrong.json()["detail"] == "price_or_stock_changed"
    cancel, _ = mutate(
        client, a, "market/listings", "listing_cancel", target_id=item["id"], target_version=2
    )
    assert cancel.status_code == 403
    mine = next(i for i in listings(client, a) if i["id"] == item["id"])
    assert mine["bought"] is True and mine["quantity"] == rotation.STOCK - 2
    with sessions() as db:
        account = db.get(Account, a["account_id"])
        assert account.state["printingCards"]["a-1#holo"] == 6
        assert account.state["marketSpentTokens"] == 1200
        assert account.state.get("marketEarnedTokens", 0) == 0
        assert {row.account_id for row in db.scalars(select(MarketPurchase))} == {
            a["account_id"],
            b["account_id"],
        }
        bought = db.scalars(select(Notification).where(Notification.kind == "listing_bought"))
        assert all(row.read for row in bought)


def test_the_last_copy_sells_out(market, monkeypatch):
    client, sessions, a, b = market
    monkeypatch.setattr(rotation, "STOCK", 1)
    item = listings(client, a)[0]
    assert buy(client, a, item).status_code == 200
    late = buy(client, b, item)
    assert late.status_code == 409 and late.json()["detail"] == "listing_unavailable"
    with sessions() as db:
        assert db.get(MarketListing, item["id"]).status == "sold"


def test_expiry_tells_nobody(market):
    client, sessions, a, _ = market
    listings(client, a)
    with sessions() as db:
        events = len(db.scalars(select(GameEvent)).all())
        for row in db.scalars(select(MarketListing)):
            row.expires_at = 1
        db.commit()
        maintenance.expire(db)
        assert {row.status for row in db.scalars(select(MarketListing))} == {"expired"}
        assert db.scalars(select(Notification)).all() == []
        assert len(db.scalars(select(GameEvent)).all()) == events


def test_wishlists_hear_about_rotation_cards(market):
    client, sessions, a, b = market
    reply, _ = mutate(client, b, "wishlist", "wishlist", wishes=[{"card_id": "a-1"}])
    assert reply.status_code == 200, reply.text
    listings(client, a)
    with sessions() as db:
        notes = db.scalars(select(Notification).where(Notification.kind == "wishlist_listing"))
        assert [row.account_id for row in notes] == [b["account_id"]]


def test_blocking_someone_does_not_hide_the_rotation(market):
    client, _, a, b = market
    second = client.get("/v1/profile", headers=headers(b)).json()["profile"]
    reply, _ = mutate(client, a, "friends", "block", target_id=second["public_id"])
    assert reply.status_code == 200, reply.text
    assert len(listings(client, a)) == 2


def test_players_listings_come_first_and_the_seller_filter_splits_them(market):
    client, sessions, a, b = market
    reply, _ = mutate(
        client, a, "market/listings", "listing_create", printing="a-1#holo", unit_tokens=300
    )
    assert reply.status_code == 200, reply.text
    with sessions() as db:
        # Listed long before this morning's rotation went up.
        db.get(MarketListing, reply.json()["result"]["id"]).created_at = 1
        db.commit()

    def page(**query):
        reply = client.get("/v1/market/listings", headers=headers(b), params=query)
        assert reply.status_code == 200, reply.text
        return reply.json()["items"]

    newest = page()
    # The player's listing leads although the rotation is newer.
    assert [item.get("rotation", False) for item in newest] == [False, True, True]
    assert [item["unit_tokens"] for item in newest[1:]] == sorted(u for _, u in OFFERS)
    assert [item["unit_tokens"] for item in page(sort="price")] == [300, 900, 1200]
    assert all(item.get("rotation") for item in page(seller="bot"))
    assert [item["unit_tokens"] for item in page(seller="players")] == [300]


def test_missing_filter_keeps_only_cards_the_viewer_has_none_of(market):
    client, _, a, b = market
    for user, lacking in [(a, "a-2#holo"), (b, "a-1#holo")]:
        reply = client.get("/v1/market/listings", headers=headers(user), params={"missing": "true"})
        assert reply.status_code == 200, reply.text
        assert [item["printing"] for item in reply.json()["items"]] == [lacking]
    # Without the filter both cards stay listed.
    assert len(listings(client, a)) == 2


def test_restock_failure_keeps_market_available_and_logs_no_private_values(market, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from test_observability import capture

    client, _, a, _ = market
    private = "private-wallet-and-session-marker"

    def broken(*_args, **_kwargs):
        raise OperationalError("UPDATE accounts SET state=?", (private,), RuntimeError(private))

    monkeypatch.setattr(rotation, "restock", broken)
    handler = capture()
    try:
        reply = client.get("/v1/market/listings", headers=headers(a))
    finally:
        logger.removeHandler(handler)
    assert reply.status_code == 200
    failures = [
        record for record in handler.records if "rotation restock failed" in record.getMessage()
    ]
    assert len(failures) == 1 and failures[0].exc_info is None
    logs = "\n".join(logging.Formatter().format(record) for record in handler.records)
    assert "OperationalError" in logs and "broken" in logs
    assert private not in logs and "UPDATE accounts" not in logs
