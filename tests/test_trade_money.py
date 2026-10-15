"""Trades with money on one side: cards plus money for a better card, and the like."""

import pytest
from test_auth import Rules, auth, headers, registered  # noqa: F401
from test_online import connect, mutate, online  # noqa: F401

from app.models import Account, CardTrade

A1 = {"printing": "a-1#holo", "quantity": 1}
A2 = {"printing": "a-2#holo", "quantity": 1}


def propose(client, user, friend, **fields):
    return mutate(client, user, "trades", "trade_create", target_id=friend["public_id"], **fields)[
        0
    ]


def accept(client, user, trade_id):
    version = next(
        item["version"]
        for item in client.get("/v1/trades", headers=headers(user)).json()["items"]
        if item["id"] == trade_id
    )
    return mutate(
        client, user, "trades", "trade_accept", target_id=trade_id, target_version=version
    )[0]


def accounts(sessions, *users):
    with sessions() as db:
        return [db.get(Account, user["account_id"]) for user in users]


@pytest.mark.parametrize(
    "fields, give, get",
    [
        # A card plus money for a better card.
        ({"offered": [A1], "requested": [A2], "offered_tokens": 3000}, 3000, 0),
        # Money for a friend's card.
        ({"requested": [A2], "offered_tokens": 2000}, 2000, 0),
        # A card for money.
        ({"offered": [A1], "requested_tokens": 1500}, 0, 1500),
    ],
)
def test_money_moves_with_the_cards(online, fields, give, get):  # noqa: F811
    client, sessions, a, b = online
    _, second = connect(client, a, b)
    reply = propose(client, a, second, **fields)
    assert reply.status_code == 200, reply.text
    trade_id = reply.json()["result"]["id"]
    shown = client.get("/v1/trades", headers=headers(b)).json()["items"][0]
    assert (shown["offered_tokens"], shown["requested_tokens"]) == (give, get)
    assert accept(client, b, trade_id).status_code == 200
    sender, recipient = accounts(sessions, a, b)
    assert sender.balance == 10000 - give + get and recipient.balance == 10000 + give - get
    assert sender.state.get("marketSpentTokens", 0) == give
    assert recipient.state.get("marketSpentTokens", 0) == get
    gave_card, got_card = bool(fields.get("offered")), bool(fields.get("requested"))
    assert sender.state["printingCards"].get("a-1#holo", 0) == 5 - gave_card
    assert sender.state["printingCards"].get("a-2#holo", 0) == int(got_card)


@pytest.mark.parametrize(
    "fields, status, detail",
    [
        (
            {"offered": [A1], "requested": [A2], "offered_tokens": 1, "requested_tokens": 1},
            422,
            "money_on_both_sides",
        ),
        ({"offered_tokens": 100}, 422, "cards_required"),
        ({"offered": [A1]}, 422, "both_sides_required"),
        (
            {"offered": [A1], "requested": [A2], "offered_tokens": 10**9},
            409,
            "insufficient_balance",
        ),
    ],
)
def test_money_rules_are_checked_when_proposing(online, fields, status, detail):  # noqa: F811
    client, _, a, b = online
    _, second = connect(client, a, b)
    reply = propose(client, a, second, **fields)
    assert reply.status_code == status and reply.json()["detail"] == detail


def spend(sessions, user, left):
    with sessions() as db:
        account = db.get(Account, user["account_id"])
        account.state = {**account.state, "usedSinceInstall": left}
        account.balance = left
        db.commit()


def test_money_is_not_held_so_acceptance_checks_both_sides_again(online):  # noqa: F811
    client, sessions, a, b = online
    _, second = connect(client, a, b)
    offered = propose(client, a, second, offered=[A1], requested=[A2], offered_tokens=3000)
    asked = propose(client, a, second, offered=[A1], requested_tokens=5000)
    spend(sessions, a, 1000)
    reply = accept(client, b, offered.json()["result"]["id"])
    assert reply.status_code == 409 and reply.json()["detail"] == "offered_tokens_unavailable"
    spend(sessions, b, 1000)
    reply = accept(client, b, asked.json()["result"]["id"])
    assert reply.status_code == 409 and reply.json()["detail"] == "insufficient_balance"
    with sessions() as db:
        assert {row.status for row in db.query(CardTrade)} == {"pending"}


def test_a_counter_offer_can_ask_for_money(online):  # noqa: F811
    client, sessions, a, b = online
    first, second = connect(client, a, b)
    original = propose(client, a, second, offered=[A1], requested=[A2]).json()["result"]["id"]
    reply = mutate(
        client,
        b,
        "trades",
        "trade_counter",
        target_id=original,
        target_version=0,
        offered=[A2],
        requested=[A1],
        requested_tokens=700,
    )[0]
    assert reply.status_code == 200, reply.text
    assert accept(client, a, reply.json()["result"]["id"]).status_code == 200
    sender, recipient = accounts(sessions, b, a)
    assert sender.balance == 10700 and recipient.balance == 9300
