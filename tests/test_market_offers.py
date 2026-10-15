"""Price offers on players' listings: below the asking price, at least half of it."""

from sqlalchemy import select
from test_auth import Rules, auth, headers, registered  # noqa: F401
from test_online import mutate, online  # noqa: F401
from test_trade_money import spend

from app import maintenance
from app.models import Account, MarketListing, MarketOffer


def list_card(client, seller, quantity=2, unit=1000):
    reply, _ = mutate(
        client,
        seller,
        "market/listings",
        "listing_create",
        printing="a-1#holo",
        quantity=quantity,
        unit_tokens=unit,
    )
    assert reply.status_code == 200, reply.text
    return reply.json()["result"]["id"]


def offer(client, buyer, listing_id, unit, quantity=1):
    reply, _ = mutate(
        client,
        buyer,
        "market/offers",
        "offer_create",
        target_id=listing_id,
        unit_tokens=unit,
        quantity=quantity,
    )
    return reply


def offers(client, user, role):
    reply = client.get("/v1/market/offers", headers=headers(user), params={"role": role})
    assert reply.status_code == 200, reply.text
    return reply.json()["items"]


def answer(client, seller, action, offer_id):
    version = next(o["version"] for o in offers(client, seller, "received") if o["id"] == offer_id)
    reply, _ = mutate(
        client, seller, "market/offers", action, target_id=offer_id, target_version=version
    )
    return reply


def notes(client, user):
    reply = client.get("/v1/notifications", headers=headers(user), params={"newest": "true"})
    return [(item["kind"], item.get("detail", {})) for item in reply.json()["items"]]


def test_an_offer_sits_below_the_price_and_at_least_at_half(online):  # noqa: F811
    client, _, a, b = online
    listing = list_card(client, a)
    for unit, quantity, status, detail in [
        (1000, 1, 409, "offer_not_below_price"),
        (499, 1, 409, "offer_too_low"),
        (600, 3, 409, "price_or_stock_changed"),
        (600, 1, 200, None),
        (500, 2, 200, None),
    ]:
        reply = offer(client, b, listing, unit, quantity)
        assert reply.status_code == status, (unit, reply.text)
        if detail:
            assert reply.json()["detail"] == detail
    own = offer(client, a, listing, 600)
    assert own.status_code == 403 and own.json()["detail"] == "offer_not_allowed"
    # The second offer replaced the first: one open offer per buyer and listing.
    assert [(o["unit_tokens"], o["quantity"]) for o in offers(client, b, "sent")] == [(500, 2)]
    received = offers(client, a, "received")
    assert [(o["unit_tokens"], o["nickname"], o["listing_unit_tokens"]) for o in received] == [
        (500, own_nickname(client, b), 1000)
    ]
    listed = client.get("/v1/market/listings", headers=headers(b)).json()["items"][0]
    assert listed["my_offer"]["unit_tokens"] == 500
    kinds = [kind for kind, _ in notes(client, a)]
    assert kinds.count("offer_received") == 2


def own_nickname(client, user):
    return client.get("/v1/profile", headers=headers(user)).json()["profile"]["nickname"]


def test_accepting_sells_at_the_offered_price(online):  # noqa: F811
    client, sessions, a, b = online
    seller_name = own_nickname(client, a)
    listing = list_card(client, a)
    offer_id = offer(client, b, listing, 800, 2).json()["result"]["id"]
    assert answer(client, a, "offer_accept", offer_id).status_code == 200
    with sessions() as db:
        seller, buyer = db.get(Account, a["account_id"]), db.get(Account, b["account_id"])
        assert seller.balance == 11600 and buyer.balance == 8400
        assert buyer.state["printingCards"]["a-1#holo"] == 2
        assert seller.state["printingCards"]["a-1#holo"] == 3
        assert db.get(MarketListing, listing).status == "sold"
        assert db.get(MarketOffer, offer_id).status == "accepted"
    kind, detail = notes(client, b)[0]
    assert kind == "offer_accepted"
    assert detail == {
        "printing": "a-1#holo",
        "unit_tokens": 800,
        "nickname": seller_name,
    }


def test_an_offer_the_stock_can_no_longer_fill_closes(online):  # noqa: F811
    client, sessions, a, b = online
    listing = list_card(client, a)
    offer_id = offer(client, b, listing, 800, 2).json()["result"]["id"]
    version = client.get("/v1/market/listings", headers=headers(b)).json()["items"][0]["version"]
    bought, _ = mutate(
        client,
        b,
        "market/listings",
        "listing_buy",
        target_id=listing,
        target_version=version,
        quantity=1,
        unit_tokens=1000,
    )
    assert bought.status_code == 200, bought.text
    with sessions() as db:
        assert db.get(MarketOffer, offer_id).status == "expired"
    assert "offer_expired" in [kind for kind, _ in notes(client, b)]


def test_reject_cancel_and_spent_money(online):  # noqa: F811
    client, sessions, a, b = online
    listing = list_card(client, a)
    first = offer(client, b, listing, 700).json()["result"]["id"]
    assert answer(client, a, "offer_reject", first).status_code == 200
    assert notes(client, b)[0][0] == "offer_rejected"
    second = offer(client, b, listing, 700).json()["result"]["id"]
    cancel, _ = mutate(client, b, "market/offers", "offer_cancel", target_id=second)
    assert cancel.status_code == 200
    late, _ = mutate(client, a, "market/offers", "offer_accept", target_id=second, target_version=1)
    assert late.status_code == 409 and late.json()["detail"] == "offer_unavailable"
    third = offer(client, b, listing, 700).json()["result"]["id"]
    # The buyer's money is not held: they spent it, so the seller cannot accept.
    spend(sessions, b, 100)
    broke = answer(client, a, "offer_accept", third)
    assert broke.status_code == 409 and broke.json()["detail"] == "buyer_cannot_pay"
    sent = [o["status"] for o in offers(client, b, "sent")]
    assert sent[0] == "pending" and sorted(sent[1:]) == ["cancelled", "rejected"]


def test_offers_close_with_their_listing_or_after_48_hours(online):  # noqa: F811
    client, sessions, a, b = online
    listing = list_card(client, a)
    offer_id = offer(client, b, listing, 700).json()["result"]["id"]
    cancel, _ = mutate(
        client, a, "market/listings", "listing_cancel", target_id=listing, target_version=0
    )
    assert cancel.status_code == 200, cancel.text
    with sessions() as db:
        assert db.get(MarketOffer, offer_id).status == "expired"
    other = list_card(client, a)
    stale = offer(client, b, other, 700).json()["result"]["id"]
    with sessions() as db:
        db.get(MarketOffer, stale).expires_at = 1
        db.commit()
        maintenance.expire(db)
        assert db.get(MarketOffer, stale).status == "expired"
        assert db.get(MarketListing, other).status == "active"
    assert [kind for kind, _ in notes(client, b)].count("offer_expired") == 2


def test_rotation_listings_take_no_offers(online, monkeypatch):  # noqa: F811
    client, sessions, a, b = online
    monkeypatch.setattr(
        Rules, "rotation_offers", lambda self, rng, prices=None: [("a-2#holo", 900)], False
    )
    items = client.get("/v1/market/listings", headers=headers(b)).json()["items"]
    rotation = next(item for item in items if item.get("rotation"))
    reply = offer(client, b, rotation["id"], 600)
    assert reply.status_code == 409 and reply.json()["detail"] == "offer_not_accepted"
    with sessions() as db:
        assert db.scalars(select(MarketOffer)).all() == []
