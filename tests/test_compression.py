from test_auth import Rules, auth, headers, registered  # noqa: F401

from app.models import Account


def test_large_replies_are_gzip_compressed_for_clients_that_accept_it(auth):  # noqa: F811
    client, sessions = auth
    user = registered(client, email="compress@example.com")
    history = [{"setID": "a", "cards": [f"a-{i}#holo" for i in range(10)]} for _ in range(500)]
    with sessions() as db:
        account = db.get(Account, user["account_id"])
        account.state = {**account.state, "openingHistory": history}
        db.commit()

    compressed = client.get(
        "/v1/state", headers={**headers(user), "Accept-Encoding": "gzip"}
    )
    plain = client.get("/v1/state", headers={**headers(user), "Accept-Encoding": "identity"})

    assert compressed.status_code == plain.status_code == 200
    assert compressed.headers["content-encoding"] == "gzip"
    assert compressed.headers["x-request-id"]
    assert "content-encoding" not in plain.headers
    assert compressed.json() == plain.json()
    assert compressed.num_bytes_downloaded < len(plain.content) / 5


def test_small_replies_stay_uncompressed(auth):  # noqa: F811
    client, _ = auth

    reply = client.get("/health", headers={"Accept-Encoding": "gzip"})

    assert reply.status_code == 200
    assert "content-encoding" not in reply.headers
