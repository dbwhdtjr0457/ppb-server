from dataclasses import replace

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app import auth_service, main


def request(peer="127.0.0.1", **headers):
    return Request(
        {
            "type": "http",
            "client": (peer, 1234),
            "headers": [
                (name.replace("_", "-").encode(), value.encode()) for name, value in headers.items()
            ],
        }
    )


def test_forwarding_header_requires_explicit_trusted_loopback_connector(monkeypatch):
    forwarded = request(cf_connecting_ip="203.0.113.20", x_forwarded_for="198.51.100.1")
    monkeypatch.setattr(
        auth_service, "settings", replace(auth_service.settings, trust_cloudflare_proxy=False)
    )
    assert auth_service.client_address(forwarded) == "127.0.0.1"
    monkeypatch.setattr(
        auth_service, "settings", replace(auth_service.settings, trust_cloudflare_proxy=True)
    )
    assert auth_service.client_address(forwarded) == "203.0.113.20"
    assert auth_service.client_address(request(x_forwarded_for="198.51.100.1")) == "127.0.0.1"
    assert (
        auth_service.client_address(request("203.0.113.1", cf_connecting_ip="198.51.100.1"))
        == "203.0.113.1"
    )
    assert (
        auth_service.client_address(request("::1", cf_connecting_ip="2001:db8::1")) == "2001:db8::1"
    )
    with pytest.raises(HTTPException) as error:
        auth_service.client_address(request(cf_connecting_ip="203.0.113.1, 203.0.113.2"))
    assert error.value.status_code == 400


def test_private_diagnostics_reject_forwarded_requests_but_allow_local_probes(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, private_diagnostics=True))
    with TestClient(main.app, client=("127.0.0.1", 1234)) as client:
        for path in ("/ready", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
            assert client.get(path, headers={"CF-Connecting-IP": "203.0.113.20"}).status_code == 404
        assert client.get("/openapi.json").status_code == 200
        assert (
            client.get("/health", headers={"CF-Connecting-IP": "203.0.113.20"}).status_code == 200
        )
    with TestClient(main.app, client=("203.0.113.20", 1234)) as client:
        assert client.get("/docs").status_code == 404

