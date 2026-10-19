"""Pending pre-0009 requests recover their committed result after deployment."""

import hashlib
import json

import pytest
from test_auth import Rules, auth, headers, registered  # noqa: F401
from test_online import mutate, online  # noqa: F401

from app.models import OnlineReceipt
from app.online_schemas import OnlineCommand


@pytest.mark.parametrize(
    "route,action,fields",
    [
        ("profile", "profile", {"nickname": "Legacy fixture"}),
        (
            "market/listings",
            "listing_create",
            {"printing": "a-1#holo", "quantity": 1, "unit_tokens": 1000},
        ),
    ],
)
def test_pre_upgrade_receipt_replays_without_changing_resources(online, route, action, fields):  # noqa: F811
    client, sessions, a, _ = online
    committed, body = mutate(client, a, route, action, **fields)
    assert committed.status_code == 200
    # Match the exact canonical shape used by the deployed 0.18 server.
    legacy = OnlineCommand.model_validate(body).model_dump(mode="json")
    del legacy["offered_tokens"], legacy["requested_tokens"]
    encoded = json.dumps({"device": a["device_id"], "command": legacy}, sort_keys=True)
    with sessions() as db:
        receipt = db.get(OnlineReceipt, (a["account_id"], body["request_id"]))
        receipt.fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        db.commit()
    before = client.get("/v1/state", headers=headers(a)).json()
    replay = client.post("/v1/" + route, headers=headers(a), json=body)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert replay.json()["result"] == committed.json()["result"]
    assert client.get("/v1/state", headers=headers(a)).json() == before
    # Compatibility is limited to absent/default-zero fields, never new terms.
    for changed in ({"offered_tokens": 1}, {"requested_tokens": 1}, {"quantity": 2}):
        rejected = client.post("/v1/" + route, headers=headers(a), json={**body, **changed})
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "idempotency_key_reused"
        assert client.get("/v1/state", headers=headers(a)).json() == before
