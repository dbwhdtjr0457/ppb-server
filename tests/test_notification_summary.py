from test_auth import Rules, auth, headers, registered  # noqa: F401
from test_online import mutate, online, own  # noqa: F401


def test_notification_summary_counts_what_needs_an_answer(online):  # noqa: F811
    client, sessions, a, b = online
    empty = client.get("/v1/notifications/summary", headers=headers(b)).json()
    assert empty == {"unread": 0, "incoming_trades": 0, "incoming_friends": 0}

    first = own(client, a)
    second = own(client, b)
    reply, _ = mutate(client, a, "friends", "friend_request", friend_code=second["friend_code"])
    assert reply.status_code == 200, reply.text
    pending = client.get("/v1/notifications/summary", headers=headers(b)).json()
    assert pending["incoming_friends"] == 1 and pending["unread"] >= 1

    request_id = reply.json()["result"]["id"]
    accepted, _ = mutate(
        client, b, "friends", "friend_accept", target_id=request_id, target_version=0
    )
    assert accepted.status_code == 200, accepted.text
    offer, _ = mutate(
        client, a, "trades", "trade_create", target_id=second["public_id"],
        offered=[{"printing": "a-1#holo", "quantity": 1}],
        requested=[{"printing": "a-2#holo", "quantity": 1}],
    )
    assert offer.status_code == 200, offer.text
    summary = client.get("/v1/notifications/summary", headers=headers(b)).json()
    assert summary["incoming_trades"] == 1 and summary["incoming_friends"] == 0
    sender = client.get("/v1/notifications/summary", headers=headers(a)).json()
    assert sender["incoming_trades"] == 0
    assert first["public_id"] != second["public_id"]
