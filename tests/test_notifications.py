from test_auth import Rules, auth, headers, registered  # noqa: F401
from test_online import connect, mutate, online  # noqa: F401


def listed(client, user, **params):
    return client.get("/v1/notifications", headers=headers(user), params=params).json()


def summary(client, user):
    return client.get("/v1/notifications/summary", headers=headers(user)).json()


def offer(client, sender, recipient_profile):
    reply, _ = mutate(
        client,
        sender,
        "trades",
        "trade_create",
        target_id=recipient_profile["public_id"],
        offered=[{"printing": "a-1#holo", "quantity": 1}],
        requested=[{"printing": "a-2#holo", "quantity": 1}],
    )
    assert reply.status_code == 200, reply.text
    return reply.json()["result"]["id"]


def test_details_name_the_other_person_and_the_card(online):  # noqa: F811
    client, _, a, b = online
    first, second = connect(client, a, b)
    for nickname, user in [("Alpha", a), ("Beta", b)]:
        reply, _ = mutate(client, user, "profile", "profile", nickname=nickname)
        assert reply.status_code == 200, reply.text
    offer(client, a, second)
    listing, _ = mutate(
        client,
        a,
        "market/listings",
        "listing_create",
        printing="a-1#holo",
        quantity=1,
        unit_tokens=123,
    )
    bought, _ = mutate(
        client,
        b,
        "market/listings",
        "listing_buy",
        target_id=listing.json()["result"]["id"],
        target_version=0,
        quantity=1,
        unit_tokens=123,
    )
    assert bought.status_code == 200, bought.text

    received = {item["kind"]: item for item in listed(client, b)["items"]}
    assert received["trade_request"]["detail"] == {"nickname": "Alpha"}
    assert received["friend_request"]["detail"] == {"nickname": "Alpha"}
    sold = {item["kind"]: item for item in listed(client, a)["items"]}["listing_sold"]
    assert sold["detail"] == {"printing": "a-1#holo", "unit_tokens": 123}
    assert first["public_id"] != second["public_id"]


def test_your_own_action_does_not_count_as_unread(online):  # noqa: F811
    client, _, a, b = online
    _, second = connect(client, a, b)
    trade = offer(client, a, second)
    before = summary(client, b)["unread"]
    accepted, _ = mutate(client, b, "trades", "trade_accept", target_id=trade, target_version=0)
    assert accepted.status_code == 200, accepted.text

    # B accepted: B keeps the line as history but has nothing new to read. A has news.
    mine = [item for item in listed(client, b)["items"] if item["kind"] == "trade_accepted"]
    assert [item["read"] for item in mine] == [True]
    assert summary(client, b)["unread"] == before
    theirs = [item for item in listed(client, a)["items"] if item["kind"] == "trade_accepted"]
    assert [item["read"] for item in theirs] == [False]

    listing, _ = mutate(
        client,
        a,
        "market/listings",
        "listing_create",
        printing="a-1#holo",
        quantity=1,
        unit_tokens=50,
    )
    unread = summary(client, b)["unread"]
    bought, _ = mutate(
        client,
        b,
        "market/listings",
        "listing_buy",
        target_id=listing.json()["result"]["id"],
        target_version=0,
        quantity=1,
        unit_tokens=50,
    )
    assert bought.status_code == 200, bought.text
    assert summary(client, b)["unread"] == unread
    assert any(
        item["kind"] == "listing_sold" and not item["read"] for item in listed(client, a)["items"]
    )


def test_read_all_marks_everything_shown_and_nothing_newer(online):  # noqa: F811
    client, _, a, b = online
    _, second = connect(client, a, b)
    offer(client, a, second)
    shown = listed(client, b, newest=True)["items"]
    assert len(shown) >= 2 and summary(client, b)["unread"] == len(shown)

    # A second offer arrives after the list was drawn; it must stay unread.
    second_trade = offer(client, a, second)
    reply, _ = mutate(
        client, b, "notifications", "notification_read_all", notification_id=shown[0]["id"]
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["result"] == {"marked": len(shown)}
    left = [item for item in listed(client, b)["items"] if not item["read"]]
    assert [item["target"] for item in left] == [second_trade]

    everything, _ = mutate(client, b, "notifications", "notification_read_all")
    assert everything.json()["result"] == {"marked": 1}
    assert summary(client, b)["unread"] == 0
    # Someone else's notifications are untouched.
    assert summary(client, a)["unread"] >= 1


def test_newest_first_pages_backwards_and_summary_reports_the_latest(online):  # noqa: F811
    client, _, a, b = online
    _, second = connect(client, a, b)
    for _ in range(3):
        offer(client, a, second)
    oldest_first = [item["id"] for item in listed(client, b)["items"]]
    assert oldest_first == sorted(oldest_first) and len(oldest_first) == 4

    page = listed(client, b, newest=True, limit=3)
    assert page["order"] == "desc" and page["next_after"] is None
    assert [item["id"] for item in page["items"]] == oldest_first[::-1][:3]
    rest = listed(client, b, before=page["next_before"], limit=3)
    assert [item["id"] for item in rest["items"]] == oldest_first[:1]
    assert rest["next_before"] is None

    assert summary(client, b)["latest_id"] == oldest_first[-1]
    # What arrived since the app last looked, oldest first: how push notifications are fetched.
    since = listed(client, b, after=oldest_first[1])
    assert since["order"] == "asc"
    assert [item["id"] for item in since["items"]] == oldest_first[2:]
