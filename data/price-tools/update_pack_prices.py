#!/usr/bin/env python3
"""Build a versioned sealed-booster price snapshot from TCGCSV.

TCGCSV is the public cached export of TCGplayer's catalogue and explicitly
permits consumers to use its JSON endpoints.  The app never performs these
requests at runtime: releases bundle the resulting snapshot so prices cannot
move underneath a running game.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any


CATEGORY_ID = 3  # TCGplayer: Pokemon
BASE_URL = "https://tcgcsv.com/tcgplayer"
USER_AGENT = "PokePackBar/0.9 market-snapshot (+https://github.com/wonyangs/PokePackBar)"

# These seven names are intentionally explicit.  Their public catalogue names
# are series labels ("XY Base Set", "SV: Scarlet & Violet 151", ...), so fuzzy
# title matching would be more dangerous than a stable TCGplayer group id.
GROUP_OVERRIDES: dict[str, int] = {
    "base1": 604,
    "ecard1": 1375,
    "pl1": 1406,
    "xy1": 1387,
    "sm1": 1863,
    "hgss1": 1402,
    "hgss2": 1399,
    "hgss3": 1403,
    "hgss4": 1381,
    "sv3pt5": 23237,
    "me1": 24380,
}


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = value.lower().replace("&", " and ")
    value = re.sub(r"pokemon|trading card game|tcg", " ", value)
    value = re.sub(r"^(ex|xy|sm|swsh|sv|me|bw|hgss|hs|pl|dp)\w*\s*[-:]?\s*", "", value)
    value = re.sub(r"\bbase set\b", " ", value)
    return "".join(character for character in value if character.isalnum())


def resolve_group(card_set: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    if group_id := GROUP_OVERRIDES.get(card_set["id"]):
        return next((group for group in groups if group["groupId"] == group_id), None)

    target = normalized_title(card_set["name"])
    if not target:
        return None

    candidates: list[tuple[int, dict[str, Any]]] = []
    for group in groups:
        candidate = normalized_title(group["name"])
        score = 0
        if candidate == target:
            score = 100
        elif len(target) >= 4 and (candidate.endswith(target) or target.endswith(candidate)):
            score = 80
        elif len(target) >= 5 and target in candidate:
            score = 60
        if group.get("isSupplemental"):
            score -= 20
        if score > 0:
            candidates.append((score, group))

    candidates.sort(key=lambda item: (-item[0], item[1]["groupId"]))
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


def booster_score(name: str) -> int:
    lowered = name.lower()
    if "booster pack" not in lowered:
        return -10_000
    if any(word in lowered for word in ("code card", "bundle", "box", "case", "blister", "lot of")):
        return -10_000

    score = 100
    if "sleeved" in lowered:
        score -= 20
    if "1st edition" in lowered or "first edition" in lowered:
        score -= 30
    if "unlimited" in lowered:
        score += 10
    return score


def choose_booster(
    products: list[dict[str, Any]], prices: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    market_by_product = {
        row["productId"]: row
        for row in prices
        if row.get("subTypeName") == "Normal"
        and isinstance(row.get("marketPrice"), (int, float))
        and row["marketPrice"] > 0
    }
    candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for product in products:
        price = market_by_product.get(product["productId"])
        score = booster_score(product["name"])
        if price is not None and score > 0:
            candidates.append((score, product, price))
    if not candidates:
        return None
    best_score = max(item[0] for item in candidates)
    best = [item for item in candidates if item[0] == best_score]
    market_price = statistics.median(item[2]["marketPrice"] for item in best)
    # 팩 아트 하나가 유난히 비싸거나 싼 세트가 있다. set 가격은 임의의 첫 상품이 아니라
    # 같은 판본·포장 조건 후보의 중앙값으로 잡고, 링크는 그 중앙값에 가장 가까운 실제
    # 상품을 대표로 남긴다.
    _, product, source_price = min(
        best,
        key=lambda item: (abs(item[2]["marketPrice"] - market_price), item[1]["productId"]),
    )
    price = dict(source_price)
    price["marketPrice"] = market_price
    product = dict(product)
    product["snapshotSampleCount"] = len(best)
    return product, price


def build_snapshot(index_path: Path, delay: float) -> tuple[dict[str, Any], list[str]]:
    card_index = json.loads(index_path.read_text())
    groups = fetch_json(f"{BASE_URL}/{CATEGORY_ID}/groups")["results"]
    entries: dict[str, Any] = {}
    missing: list[str] = []

    for position, card_set in enumerate(card_index["sets"]):
        group = resolve_group(card_set, groups)
        if group is None:
            missing.append(f"{card_set['id']}: no unambiguous TCGplayer group")
            continue

        group_id = group["groupId"]
        products = fetch_json(f"{BASE_URL}/{CATEGORY_ID}/{group_id}/products")["results"]
        prices = fetch_json(f"{BASE_URL}/{CATEGORY_ID}/{group_id}/prices")["results"]
        selected = choose_booster(products, prices)
        if selected is None:
            missing.append(f"{card_set['id']}: no priced single booster pack in {group['name']}")
        else:
            product, price = selected
            entries[card_set["id"]] = {
                "usd": round(float(price["marketPrice"]), 2),
                "productID": product["productId"],
                "productName": product["name"],
                "url": product["url"],
                "sampleCount": product["snapshotSampleCount"],
            }

        if position + 1 < len(card_index["sets"]):
            time.sleep(delay)

    snapshot = {
        "version": 1,
        "asOf": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        "currency": "USD",
        "source": "TCGplayer market price via TCGCSV",
        "packs": dict(sorted(entries.items())),
    }
    return snapshot, missing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, default=Path("Sources/PokePackBar/Resources/card-index.json"))
    parser.add_argument("--output", type=Path, default=Path("Sources/PokePackBar/Resources/pack-prices.json"))
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()

    snapshot, missing = build_snapshot(args.index, max(0, args.delay))
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {len(snapshot['packs'])} pack prices to {args.output}")
    for message in missing:
        print(f"missing: {message}")


if __name__ == "__main__":
    main()
