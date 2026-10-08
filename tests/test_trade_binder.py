from sqlalchemy import select
from test_auth import Rules, auth, headers, registered  # noqa: F401
from test_online import connect, mutate, online, own  # noqa: F401

from app.models import Account, CardTrade, Reservation


def binder(client, user, friend):
    return client.get(f"/v1/friends/{friend['public_id']}/tradeable", headers=headers(user))


def propose(client, user, friend, offered, requested):
    reply, _ = mutate(
        client,
        user,
        "trades",
        "trade_create",
        target_id=friend["public_id"],
        offered=[{"printing": key, "quantity": n} for key, n in offered.items()],
        requested=[{"printing": key, "quantity": n} for key, n in requested.items()],
    )
    return reply


def test_trade_binder_lists_spares_only_for_friends_who_share_them(online):  # noqa: F811
    client, sessions, a, b = online
    stranger = own(client, b)
    assert binder(client, a, stranger).status_code == 404
    _, friend = connect(client, a, b)
    reply = binder(client, a, friend)
    assert reply.status_code == 200
    # Five copies, one always kept.
    assert reply.json() == {"items": {"a-2#holo": 4}}
    assert own(client, b)["trade_list_public"] is True
    hidden, _ = mutate(client, b, "profile", "profile", nickname="Beta", trade_list_public=False)
    assert hidden.status_code == 200, hidden.text
    assert binder(client, a, friend).status_code == 403
    assert binder(client, a, friend).json()["detail"] == "trade_list_private"
    # An older app that does not send the flag keeps the stored choice.
    kept, _ = mutate(client, b, "profile", "profile", nickname="Beta2")
    assert kept.status_code == 200
    assert own(client, b)["trade_list_public"] is False


def test_proposals_ask_only_for_spares_a_sharing_friend_can_give(online):  # noqa: F811
    client, sessions, a, b = online
    _, friend = connect(client, a, b)
    too_many = propose(client, a, friend, {"a-1#holo": 1}, {"a-2#holo": 5})
    assert too_many.status_code == 409
    assert too_many.json()["detail"] == "requested_cards_unavailable"
    missing = propose(client, a, friend, {"a-1#holo": 1}, {"a-1#normal": 1})
    assert missing.status_code == 409
    with sessions() as db:
        assert db.scalar(select(Reservation)) is None
    assert propose(client, a, friend, {"a-1#holo": 1}, {"a-2#holo": 4}).status_code == 200
    # Spares kept private are not checked, so proposals cannot probe what they own.
    mutate(client, b, "profile", "profile", nickname="Beta", trade_list_public=False)
    assert propose(client, a, friend, {"a-1#holo": 1}, {"a-1#normal": 1}).status_code == 200


def test_counter_offer_closes_the_original_and_sends_new_cards_back(online):  # noqa: F811
    client, sessions, a, b = online
    _, friend = connect(client, a, b)
    created = propose(client, a, friend, {"a-1#holo": 2}, {"a-2#holo": 2})
    original = created.json()["result"]["id"]

    def counter(user, offered, requested, version=0):
        return mutate(
            client,
            user,
            "trades",
            "trade_counter",
            target_id=original,
            target_version=version,
            offered=[{"printing": key, "quantity": n} for key, n in offered.items()],
            requested=[{"printing": key, "quantity": n} for key, n in requested.items()],
        )[0]

    assert counter(a, {"a-1#holo": 1}, {"a-2#holo": 1}).status_code == 403
    # Asking for more than the original sender can spare is refused like a new proposal.
    refused = counter(b, {"a-2#holo": 1}, {"a-1#holo": 5})
    assert refused.status_code == 409
    with sessions() as db:
        assert db.get(CardTrade, original).status == "pending"
        assert db.scalar(select(Reservation).where(Reservation.owner == original)) is not None
    reply = counter(b, {"a-2#holo": 1}, {"a-1#holo": 3})
    assert reply.status_code == 200, reply.text
    answer = reply.json()["result"]["id"]
    with sessions() as db:
        old = db.get(CardTrade, original)
        new = db.get(CardTrade, answer)
        assert old.status == "countered"
        assert db.scalar(select(Reservation).where(Reservation.owner == original)) is None
        assert (new.sender, new.recipient) == (b["account_id"], a["account_id"])
        assert new.counter_of == original
        assert new.offered == {"a-2#holo": 1} and new.requested == {"a-1#holo": 3}
    assert counter(b, {"a-2#holo": 1}, {"a-1#holo": 1}, version=1).status_code == 409
    listed = client.get("/v1/trades", headers=headers(a)).json()["items"]
    incoming = next(item for item in listed if item["id"] == answer)
    assert incoming["incoming"] is True and incoming["counter_of"] == original
    accepted, _ = mutate(client, a, "trades", "trade_accept", target_id=answer, target_version=0)
    assert accepted.status_code == 200, accepted.text
    with sessions() as db:
        alpha = db.get(Account, a["account_id"]).state["printingCards"]
        beta = db.get(Account, b["account_id"]).state["printingCards"]
        assert alpha == {"a-1#holo": 2, "a-2#holo": 1}
        assert beta == {"a-2#holo": 4, "a-1#holo": 3}
