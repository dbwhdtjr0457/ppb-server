#!/usr/bin/env python3
"""Generate public Python rules data from a built Swift catalogue oracle.

This is deliberately a developer build command, never imported by the service.
The generated files are public static assets; no installed wallet is accessed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument(
        "--source-directory",
        type=Path,
        required=True,
        help="PokePackBar checkout containing reviewed price collector scripts",
    )
    args = parser.parse_args()
    environment = os.environ.copy()
    # The exporter is authoritative public data, not the developer's optional
    # local imported snapshot. Also omit server runtime credentials/overrides.
    environment.pop("PPB_RULE_PRICES", None)
    environment["PPB_SERVER_URL"] = "https://export.invalid"
    subprocess.run(
        [str(args.executable.resolve()), "--export-server-data", str(args.output.resolve())],
        check=True,
        timeout=180,
        env=environment,
    )
    # The rules data carries no Korean names; market and inventory search need them.
    exported = subprocess.run(
        [str(args.executable.resolve()), "--export-online-catalogue"],
        check=True,
        capture_output=True,
        timeout=180,
        env=environment,
    )
    catalogue = json.loads(exported.stdout)
    names = {entry["id"]: entry["name_ko"] for entry in catalogue if entry.get("name_ko")}
    (args.output / "card-names-ko.json").write_text(
        json.dumps(names, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    price_tools = args.output / "price-tools"
    price_tools.mkdir(parents=True, exist_ok=True)
    for name in (
        "update_printing_prices.py",
        "update_pack_prices.py",
        "curated-price-references.json",
    ):
        shutil.copyfile(args.source_directory / "scripts" / name, price_tools / name)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.native_data import load

    data = load(args.output)
    print(
        f"Exported {len(data['cards'])} cards ({len(names)} Korean names), "
        f"{len(data['sets'])} sets: {data['rules_version']}"
    )


if __name__ == "__main__":
    main()
