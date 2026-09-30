#!/usr/bin/env python3
"""Fetch exact EN printings, joined by expansion + collector number + card name.

No rarity multipliers, first-edition premiums, promo substitutions or missing-price
invention. The native resolver supplies canonical finishes instead of duplicating
its rarity rules here. Produces bundled data and an independently importable pair.
"""
import argparse
import concurrent.futures
import datetime as dt
import email.utils
import json
import math
import re
import subprocess
import unicodedata
import urllib.request
from pathlib import Path

from update_pack_prices import choose_booster, resolve_group

BASE = "https://tcgcsv.com/tcgplayer/3"


def atomic_json(path, payload, **options):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, **options) + "\n")
    temporary.replace(path)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "PokePackBar/local-price-import"})
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.load(response)
        modified = response.headers.get("Last-Modified")
    if payload.get("success") is not True:
        raise ValueError(f"TCGCSV failure: {url}")
    date = email.utils.parsedate_to_datetime(modified).date().isoformat() if modified else dt.datetime.now(dt.timezone.utc).date().isoformat()
    return payload["results"], date


def name_key(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value).lower() if c.isalnum() and not unicodedata.combining(c))


def number_key(value):
    if re.fullmatch(r"[RGB][/_]RGB", value.strip(), re.I):
        return value.strip().upper().replace("_", "/")
    match = re.fullmatch(r"([A-Za-z]*)(\d+)(?:/[^ ]+)?", value.strip())
    return match[1].upper() + str(int(match[2])) if match else None


def valid_price(value):
    return type(value) in (float, int) and math.isfinite(value) and 0 < value < 10_000_000


def match_printings(products, prices, as_of, cards, finishes, evidence, parallels):
    products = {p["productId"]: p for p in products}
    by_number = {}
    by_product = {}
    for card in cards:
        source = evidence.get(card[0], {})
        number = number_key(source.get("printedNumber") or card[0].split("-", 1)[1])
        if number is not None:
            by_number.setdefault(number, []).append(card)
        if source.get("productID"):
            by_product[source["productID"]] = (card, None)
        if parallel := parallels.get(card[0]):
            by_product[parallel["energy"]["productID"]] = (card, "reverseHolo")
            by_product[parallel["productID"]] = (card, "patternedReverse")
    candidates = {}
    for row in prices:
        price = row.get("marketPrice")
        if not valid_price(price):
            continue
        product = products.get(row["productId"], {})
        extended = {field["name"]: field.get("value", "") for field in product.get("extendedData", [])}
        linked = by_product.get(row["productId"])
        name = product.get("name", "")
        parallel = linked[1] if linked else None
        for suffix, finish in [(" (Poke Ball Pattern)", "pokeBall"), (" (Master Ball Pattern)", "masterBall")]:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                parallel = finish
        # Numeric disambiguators are not editions. Other suffixes are deliberately rejected.
        name = re.sub(r"\s*-\s*(?:\d+[A-Za-z]*/\d+|[RGB]/RGB)$", "", name)
        suffix = re.search(r"\s*\(([A-Za-z]*\d+)\)$", name)
        if suffix and number_key(suffix[1]) == number_key(extended.get("Number", "")):
            name = name[:suffix.start()]
        if linked:
            # IDs are curated from this exact expansion, including reprint ordinals
            # that intentionally differ from their historical printed number.
            card = linked[0]
        else:
            matches = [c for c in by_number.get(number_key(extended.get("Number", "")), [])
                       if name_key(name) == name_key(c[1])]
            if len(matches) != 1:
                continue
            card = matches[0]
        subtype = row.get("subTypeName")
        if parallel and subtype in ("Holofoil", "Reverse Holofoil"):
            finish = parallel
        elif parallel:
            continue
        elif subtype in ("Normal", "Unlimited Normal", "Unlimited"):
            finish = "normal"
        elif subtype in ("Holofoil", "Unlimited Holofoil"):
            finish = finishes.get(card[0])
            if finish == "normal":
                finish = "holo"
        elif subtype == "Reverse Holofoil":
            finish = "reverseHolo"
        else:
            continue
        if finish:
            candidates.setdefault(f"{card[0]}#{finish}", []).append((price, product.get("url", ""), as_of, "market"))
    exact = {key: values[0] for key, values in candidates.items() if len(values) == 1}
    return exact, sum(len(v) > 1 for v in candidates.values())


def import_set(card_set, group, cards, finishes, evidence, parallels):
    products, _ = fetch(f"{BASE}/{group['groupId']}/products")
    prices, as_of = fetch(f"{BASE}/{group['groupId']}/prices")
    exact, ambiguous = match_printings(products, prices, as_of, cards, finishes, evidence, parallels)
    selected = choose_booster(products, prices)
    pack = None
    if selected and valid_price(selected[1]["marketPrice"]):
        product, price = selected
        pack = dict(usd=round(price["marketPrice"], 2), productID=product["productId"],
                    productName=product["name"], url=product["url"],
                    sampleCount=product["snapshotSampleCount"], asOf=as_of)
    return exact, ambiguous, pack


def apply_quotes(payload, entries, finishes):
    for key, (price, url, as_of, kind) in entries.items():
        payload["printingPrices"][key] = price
        payload["printingSources"][key] = url
        payload["printingDates"][key] = as_of
        payload["printingKinds"][key] = kind
        card_id, finish = key.rsplit("#", 1)
        # A parallel premium must never become the price of the base printing.
        if finish == finishes.get(card_id):
            payload["prices"][card_id] = price
            payload["priceSources"][card_id] = url
            payload["priceDates"][card_id] = as_of
            payload["priceKinds"][card_id] = kind


def add_fallbacks(payload, fallbacks, fresh, finishes, evidence):
    applied = []
    for card_id, quote in fallbacks.items():
        key = f"{card_id}#{finishes[card_id]}"
        if quote["productID"] != evidence[card_id]["productID"] or not valid_price(quote["usd"]):
            raise ValueError(f"Invalid curated reference: {card_id}")
        # Never replace a current OR retained primary-market quote with a reference.
        if key in fresh or (key in payload["printingPrices"]
                           and payload["printingKinds"].get(key) != "completed-sales-estimate"):
            continue
        apply_quotes(payload, {key: (quote["usd"], quote["url"], quote["asOf"], "completed-sales-estimate")}, finishes)
        applied.append(card_id)
    return applied


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=".build/release/PokePackBar")
    parser.add_argument("--printing-map", type=Path,
                        help="Pre-exported canonical finishes; avoids a native executable at runtime")
    parser.add_argument("--resources", type=Path, default=Path("Sources/PokePackBar/Resources"))
    parser.add_argument("--output", type=Path, default=Path("Sources/PokePackBar/Resources/card-prices.json"))
    parser.add_argument("--snapshot", type=Path, default=Path("build/price-snapshot.json"))
    parser.add_argument("--packs-output", type=Path, default=Path("Sources/PokePackBar/Resources/pack-prices.json"))
    args = parser.parse_args()
    index = json.loads((args.resources / "card-index.json").read_text())
    payload = json.loads(args.output.read_text())
    packs = json.loads(args.packs_output.read_text())
    catalogue = json.loads((args.resources / "catalogue-sources.json").read_text())
    parallels = json.loads((args.resources / "expansion-foil.json").read_text())["ascendedParallels"]
    fallbacks = json.loads((Path(__file__).parent / "curated-price-references.json").read_text())["cards"]
    finishes = (json.loads(args.printing_map.read_text()) if args.printing_map
                else json.loads(subprocess.check_output([args.binary, "--export-printing-map"], text=True)))
    groups, _ = fetch(f"{BASE}/groups")
    for field in ("printingPrices", "printingDates", "printingSources", "printingKinds", "priceDates", "priceSources", "priceKinds"):
        payload.setdefault(field, {})
    old_date = payload.get("printingPriceSnapshot", {}).get("asOf", payload["asOf"])
    for key in payload["printingPrices"]:
        payload["printingDates"].setdefault(key, old_date)
    for card_id in payload["prices"]:
        payload["priceDates"].setdefault(card_id, payload["asOf"])
    for pack in packs["packs"].values():
        pack.setdefault("asOf", packs["asOf"])
    report = {}
    fresh = set()
    sets = {s["id"]: s for s in index["sets"]}
    for set_id in catalogue["sets"]:
        sets.setdefault(set_id, {"id": set_id, "name": set_id})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}
        for card_set in sets.values():
            source = catalogue["sets"].get(card_set["id"])
            group = (next((g for g in groups if g["groupId"] == source["groupID"]), None)
                     if source else resolve_group(card_set, groups))
            if group is None:
                report[card_set["id"]] = {"skipped": "No unambiguous TCGplayer expansion"}
                continue
            cards = [c for c in index["cards"] if c[0].split("-", 1)[0] == card_set["id"]]
            future = executor.submit(import_set, card_set, group, cards, finishes, catalogue["cards"], parallels)
            futures[future] = card_set["id"]
        for future in concurrent.futures.as_completed(futures):
            set_id = futures[future]
            try:
                entries, ambiguous, pack = future.result()
                apply_quotes(payload, entries, finishes)
                fresh.update(entries)
                if pack:
                    packs["packs"][set_id] = pack
                report[set_id] = {"imported": len(entries), "ambiguousSkipped": ambiguous, "packUpdated": pack is not None}
                print(f"{set_id}: {len(entries)} exact printings, {ambiguous} ambiguous skipped", flush=True)
            except Exception as error:
                report[set_id] = {"skipped": str(error)}
                print(f"{set_id}: kept old data ({error})", flush=True)
    if not any(entry.get("imported", 0) > 0 for entry in report.values()):
        raise RuntimeError("No source prices imported; no output written")
    references = add_fallbacks(payload, fallbacks, fresh, finishes, catalogue["cards"])
    refreshed = dt.datetime.now(dt.timezone.utc).isoformat()
    payload.update(version=5, lastRefresh=refreshed,
        printingPriceSnapshot={"asOf": max(payload["printingDates"].values()),
            "source": "TCGplayer market via TCGCSV; explicitly labelled curated completed-sales references",
            "freshPrintingCount": len(fresh), "referenceCards": references, "sets": report})
    packs["lastRefresh"] = refreshed
    packs["refreshReport"] = report
    atomic_json(args.output, payload, indent=0)
    atomic_json(args.packs_output, packs, indent=2)
    snapshot = {"schemaVersion": 1, "cardPrices": payload,
                "packPrices": packs}
    atomic_json(args.snapshot, snapshot)
    print(f"TOTAL: {len(fresh)} fresh printings, {len(references)} references, {len(payload['printingPrices'])} retained+fresh; snapshot: {args.snapshot}")


if __name__ == "__main__":
    main()
