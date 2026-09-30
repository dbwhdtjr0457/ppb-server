from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from test_auth import PASSWORD, credentials, headers, registered
from test_auth import auth as auth_fixture

from app.models import RecoveryCode

auth = auth_fixture


def test_private_device_listing_rename_revoke(auth):
    client, _ = auth
    first = registered(client, device_name="집 Mac")
    second = client.post("/auth/login", json=credentials(device_name="노트북")).json()
    stranger = registered(client, email="other@example.com")
    rows = client.get("/auth/devices", headers=headers(first)).json()["items"]
    assert {row["name"] for row in rows} == {"집 Mac", "노트북"}
    assert len([row for row in rows if row["current"]]) == 1
    target = second["device_id"]
    assert (
        client.post(
            f"/auth/devices/{target}/rename", headers=headers(stranger), json={"name": "hijack"}
        ).status_code
        == 404
    )
    assert (
        client.post(f"/auth/devices/{target}/revoke", headers=headers(stranger)).status_code == 204
    )
    assert client.get("/auth/me", headers=headers(second)).status_code == 200
    assert (
        client.post(
            f"/auth/devices/{target}/rename", headers=headers(first), json={"name": "여행용"}
        ).status_code
        == 204
    )
    assert client.post(f"/auth/devices/{target}/revoke", headers=headers(first)).status_code == 204
    assert client.get("/auth/me", headers=headers(second)).status_code == 401
    assert client.get("/auth/me", headers=headers(first)).status_code == 200
    assert client.get("/auth/devices").status_code in (401, 422)


def test_recovery_regeneration_single_use_and_revocation(auth):
    client, sessions = auth
    user = registered(client)

    def issue():
        reply = client.post("/auth/recovery", headers=headers(user), json={"password": PASSWORD})
        assert reply.headers["cache-control"] == "no-store"
        assert reply.status_code == 200
        return reply.json()["code"]

    old, code = issue(), issue()
    with sessions() as db:
        row = db.scalar(select(RecoveryCode))
        assert row.code_hash != code and len(row.code_hash) == 64
    payload = {"email": user["email"], "code": old, "new_password": "newpass8"}
    assert client.post("/auth/recover", json=payload).status_code == 401
    payload["code"] = code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: client.post("/auth/recover", json=payload), range(2)))
    assert sorted(r.status_code for r in results) == [204, 401]
    assert client.get("/auth/me", headers=headers(user)).status_code == 401
    assert client.post("/auth/login", json=credentials()).status_code == 401
    assert client.post("/auth/login", json=credentials(password="newpass8")).status_code == 200


def test_recovery_requires_password_no_enumeration_and_no_secret_echo(auth):
    client, _ = auth
    user = registered(client)
    assert (
        client.post("/auth/recovery", headers=headers(user), json={"password": "wrong"}).status_code
        == 401
    )
    payload = {"email": user["email"], "code": "a" * 43, "new_password": "password8"}
    known = client.post("/auth/recover", json=payload)
    unknown = client.post("/auth/recover", json={**payload, "email": "missing@example.com"})
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()
    bad = client.post("/auth/recover", json={**payload, "new_password": "secret"})
    assert bad.status_code == 422 and "secret" not in bad.text
    assert client.get("/v1/server/status", headers=headers(user)).status_code == 200
    assert client.get("/v1/server/status").status_code in (401, 422)


def test_password_change_invalidates_recovery(auth):
    client, _ = auth
    user = registered(client)
    code = client.post("/auth/recovery", headers=headers(user), json={"password": PASSWORD}).json()[
        "code"
    ]
    assert (
        client.post(
            "/auth/change-password",
            headers=headers(user),
            json={
                "current_password": PASSWORD,
                "new_password": "changed8",
            },
        ).status_code
        == 204
    )

    assert (
        client.post(
            "/auth/recover",
            json={
                "email": user["email"],
                "code": code,
                "new_password": "restored8",
            },
        ).status_code
        == 401
    )


def test_collector_policy_baseline_and_noncollector_highwater(auth):
    from uuid import uuid4

    client, _ = auth
    user = registered(client)
    second = client.post("/auth/login", json=credentials()).json()

    def report(who, total):
        revision = client.get("/v1/state", headers=headers(who)).json()["revision"]
        response = client.post(
            "/v1/commands",
            headers=headers(who),
            json={
                "request_id": str(uuid4()),
                "expected_revision": revision,
                "command": {"kind": "report_tokens", "collected_total": total},
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["result"]

    assert report(user, 100)["credited"] == 100
    body = {"password": PASSWORD, "expected_version": 0, "collector_device_id": user["device_id"]}
    assert client.post("/auth/token-policy", headers=headers(user), json=body).status_code == 200
    assert report(user, 200)["credit_status"] == "policy_baseline"
    assert report(user, 220)["credited"] == 20
    assert report(second, 1000)["credited"] == 0
    assert report(second, 1200)["credit_status"] == "other_collector_device"
    assert (
        client.post(
            "/auth/token-policy", headers=headers(second), json={**body, "expected_version": 1}
        ).status_code
        == 403
    )
    assert client.post("/auth/token-policy", headers=headers(user), json=body).status_code == 409
    assert (
        client.post(
            "/auth/token-policy",
            headers=headers(user),
            json={"password": PASSWORD, "expected_version": 1, "collector_device_id": None},
        ).status_code
        == 200
    )
    assert report(second, 1300)["credited"] == 0
    assert report(second, 1310)["credited"] == 10
    assert client.get("/v1/state", headers=headers(user)).json()["balance"] == 130
