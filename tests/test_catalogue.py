import json
from types import SimpleNamespace

from test_auth import Rules, auth, headers, registered  # noqa: F401

from app import catalogue
from app.models import Account
from app.native_rules import PythonRules


def test_python_rules_cards_get_korean_names_from_data():
    card = catalogue.describe("base1-1#holo", PythonRules("./data"))

    assert card["name"] == "Alakazam"
    assert card["name_ko"] == "후딘"
    assert card["printing"] == "base1-1#holo" and card["finish"] == "holo"


def test_missing_korean_name_falls_back_instead_of_failing(tmp_path):
    (tmp_path / "card-names-ko.json").write_text(json.dumps({"a-1": "알파"}), encoding="utf-8")
    rules = SimpleNamespace(
        catalogue={"a-1": {"id": "a-1", "name": "Alpha"}, "a-2": {"id": "a-2", "name": "Beta"}},
        resources_dir=tmp_path,
    )

    assert catalogue.describe("a-1#holo", rules)["name_ko"] == "알파"
    assert catalogue.describe("a-2#holo", rules)["name_ko"] == "Beta"
    assert catalogue.describe("zz-9#holo", rules)["name_ko"] == "zz-9"
    no_file = SimpleNamespace(catalogue=rules.catalogue, resources_dir=tmp_path / "missing")
    assert catalogue.describe("a-1#holo", no_file)["name_ko"] == "Alpha"


def test_inventory_and_market_survive_catalogue_without_korean_names(auth, monkeypatch):  # noqa: F811
    client, sessions = auth
    monkeypatch.setattr(
        Rules,
        "catalogue",
        {"a-1": {"id": "a-1", "name": "Alpha", "tier": "R", "set_id": "a"}},
        raising=False,
    )
    user = registered(client, email="nameless@example.com")
    with sessions() as db:
        account = db.get(Account, user["account_id"])
        account.state = {**account.state, "cards": {"a-1": 3}, "printingCards": {"a-1#holo": 3}}
        db.commit()

    inventory = client.get("/v1/inventory?q=alp", headers=headers(user))
    listings = client.get("/v1/market/listings?q=alp", headers=headers(user))

    assert inventory.status_code == 200, inventory.text
    assert inventory.json()["items"][0]["name_ko"] == "Alpha"
    assert listings.status_code == 200, listings.text
