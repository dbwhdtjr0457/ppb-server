"""Server-native trainer levels, matching the app's LevelRules.

The level comes from lifetime packs opened (`packsOpened`). Everything here is
integer arithmetic so macOS and Linux agree on every boundary. If a value
changes, change Sources/PokePackBar/Core/LevelRules.swift in the app too.
"""

from fastapi import HTTPException

MAX_LEVEL = 100
# Recent main sets with comparable pack prices. Picking by level number keeps
# rewards deterministic and stops "open one expensive pack, then claim".
REWARD_SET_IDS = (
    "sv1",
    "sv2",
    "sv3",
    "sv4",
    "sv5",
    "sv6",
    "sv7",
    "sv8",
    "sv9",
    "sv10",
    "me1",
    "me2",
)
TITLE_LEVELS = (10, 25, 50, 75, 100)
COUPON_VALUE = 0.5
COUPON_COUNT = 2


def _require(condition):
    if not condition:
        raise HTTPException(409, "game_precondition_failed")


def packs_required(level: int) -> int:
    level = min(max(level, 1), MAX_LEVEL)
    return 2 * level * (level - 1)


def level_for(packs_opened: int) -> int:
    level = 1
    while level < MAX_LEVEL and packs_opened >= packs_required(level + 1):
        level += 1
    return level


def reward(level: int) -> dict:
    return {
        "level": level,
        "setID": REWARD_SET_IDS[(level * 7) % len(REWARD_SET_IDS)],
        "packs": min(5, 1 + level // 10),
        "coupons": COUPON_COUNT if level % 5 == 0 else 0,
    }


def claimable(state) -> list[int]:
    level = level_for(state.get("packsOpened", 0))
    claimed = set(state.get("claimedLevels", ()))
    return [n for n in range(2, level + 1) if n not in claimed]


def claim_levels(state, command, ctx):
    pending = claimable(state)
    _require(pending)
    packs = state.setdefault("packs", {})
    coupons = state.setdefault("coupons", [])
    claimed = state.setdefault("claimedLevels", [])
    for level in pending:
        grant = reward(level)
        set_id = grant["setID"]
        _require(set_id in ctx.sets_by_id)
        packs[set_id] = packs.get(set_id, 0) + grant["packs"]
        if grant["coupons"]:
            existing = next(
                (
                    coupon
                    for coupon in coupons
                    if coupon["setID"] == set_id and coupon["value"] == COUPON_VALUE
                ),
                None,
            )
            if existing is None:
                coupons.append({"setID": set_id, "value": COUPON_VALUE, "left": grant["coupons"]})
            else:
                existing["left"] += grant["coupons"]
        claimed.append(level)
    return {"levels": pending}


def title_available(state, level_title) -> bool:
    return level_title in TITLE_LEVELS and level_for(state.get("packsOpened", 0)) >= level_title
