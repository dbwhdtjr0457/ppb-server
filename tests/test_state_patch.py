from copy import deepcopy

from test_game import game, headers, request  # noqa: F401

from app.game_service import history_patch, state_digest, state_patch


def apply_patch(state: dict, patch: dict) -> dict:
    """What the app does with a snapshot_patch."""
    state = deepcopy(state)
    state.update(deepcopy(patch["set"]))
    for key, change in patch.get("merge", {}).items():
        merged = dict(state.get(key, {}))
        merged.update(deepcopy(change["set"]))
        for removed in change["remove"]:
            merged.pop(removed, None)
        state[key] = merged
    for key in patch["remove"]:
        state.pop(key, None)
    if "history" in patch:
        history = state.get("openingHistory", [])[patch["history"]["drop"] :]
        state["openingHistory"] = history + deepcopy(patch["history"]["append"])
    return state


def record(n):
    return {"id": f"pack-{n}", "printings": [f"card-{n}#holo"], "seed": 10**19 + n}


def test_patch_reproduces_counters_and_capped_history():
    before = {
        "usedSinceInstall": 5,
        "packs": {"a": 1},
        "openingHistory": [record(i) for i in range(5)],
    }
    after = deepcopy(before)
    after["usedSinceInstall"] = 9
    after["packs"] = {"b": 2}
    after["openingHistory"] = [record(i) for i in range(2, 8)]  # dropped 2, appended 3

    patch = state_patch(before, after)

    assert patch["set"] == {"usedSinceInstall": 9}
    assert patch["merge"] == {"packs": {"set": {"b": 2}, "remove": ["a"]}}
    assert patch["history"]["drop"] == 2
    assert [entry["id"] for entry in patch["history"]["append"]] == ["pack-5", "pack-6", "pack-7"]
    assert apply_patch(before, patch) == after


def test_history_edge_cases_fall_back_to_a_full_value():
    old = [record(i) for i in range(3)]
    assert history_patch(old, [record(i) for i in range(10, 12)]) == {
        "drop": 3,
        "append": [record(10), record(11)],
    }
    assert history_patch([], [record(0)]) == {"drop": 0, "append": [record(0)]}
    rewritten = [record(1), {**record(2), "seed": 0}]
    assert history_patch(old, rewritten) is None
    before = {"openingHistory": old, "legacy": True}
    after = {"openingHistory": rewritten}
    patch = state_patch(before, after)
    assert patch["set"]["openingHistory"] == rewritten and patch["remove"] == ["legacy"]
    assert apply_patch(before, patch) == after


def test_command_reply_patch_matches_the_full_state(game):  # noqa: F811
    client, _ = game
    who = headers()
    first = client.post(
        "/v1/commands",
        headers={**who, "X-PPB-State-Patch": "1"},
        json=request({"kind": "report_tokens", "collected_total": 100}),
    ).json()
    # No account yet: a patch has nothing to apply to, so the full snapshot comes back.
    assert "snapshot_patch" not in first
    held = first["snapshot"]
    assert held["state_digest"] == state_digest(held["state"])

    legacy = client.post(
        "/v1/commands",
        headers=who,
        json=request({"kind": "report_tokens", "collected_total": 150}, revision=held["revision"]),
    ).json()
    assert "snapshot" in legacy and "snapshot_patch" not in legacy
    held = legacy["snapshot"]

    reply = client.post(
        "/v1/commands",
        headers={**who, "X-PPB-State-Patch": "1"},
        json=request({"kind": "report_tokens", "collected_total": 400}, revision=held["revision"]),
    ).json()
    patch = reply["snapshot_patch"]
    full = client.get("/v1/state", headers=who).json()

    assert "snapshot" not in reply
    assert patch["base_revision"] == held["revision"] and patch["revision"] == full["revision"]
    assert patch["base_digest"] == held["state_digest"]
    assert patch["state_digest"] == full["state_digest"]
    assert patch["balance"] == full["balance"]
    assert apply_patch(held["state"], patch) == full["state"]
    assert set(patch["set"]) <= {"usedSinceInstall"} and not patch["merge"]
