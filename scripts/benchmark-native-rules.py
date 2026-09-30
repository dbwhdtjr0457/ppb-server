#!/usr/bin/env python3
"""Measure the unmodified HTTP quote/commit contract on a disposable database.

No production database, credentials or API is used. Reports cold initialization,
warm request percentiles and the existing server's compute/lock diagnostics.
"""

import argparse
import hashlib
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

os.environ["PPB_JOBS_ENABLED"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import inventory
from app.database import Base, get_db, make_engine
from app.game_api import get_rules
from app.game_service import balance, initial_state
from app.main import app
from app.models import Account, LoginSession
from app.native_rules import PythonRules
from app.performance import summary


def percentiles(samples):
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "p50_ms": round(ordered[len(ordered) // 2], 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "max_ms": round(max(ordered), 3),
    }


def benchmark(resources, repetitions, collection_size):
    with tempfile.TemporaryDirectory(prefix="ppb-native-benchmark-") as directory:
        engine = make_engine(f"sqlite:///{directory}/isolated.sqlite3")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        rules = PythonRules(resources)
        began = time.perf_counter()
        state, _, version = rules.apply(initial_state(), {"kind": "initialize"})
        state["perkTokens"] = 10**14
        for card in list(rules.catalogue.values())[:collection_size]:
            state["cards"][card["id"]] = 3
            state["printingCards"][f"{card['id']}#{card['default_finish']}"] = 3
            state["cardFirstAt"][card["id"]] = 1
        cold_ms = (time.perf_counter() - began) * 1000
        account_id, device_id = str(uuid4()), str(uuid4())
        token = secrets.token_urlsafe(32)
        with sessions() as db:
            account = Account(id=account_id, revision=0, state=state, balance=balance(state))
            db.add(account)
            db.flush()
            inventory.sync(db, account)
            db.add(
                LoginSession(
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    account_id=account_id,
                    device_id=device_id,
                    expires_at=int(time.time()) + 3600,
                    revoked=False,
                )
            )
            db.commit()

        def database():
            with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_rules] = lambda: rules
        headers = {"Authorization": f"Bearer {token}", "X-PPB-Device-ID": device_id}
        quotes, commits, totals = [], [], []
        try:
            with TestClient(app) as client:
                price_version = client.get("/v1/prices", headers=headers).json()["version"]
                # First request compiles the API's published price object separately.
                for revision in range(repetitions + 1):
                    body = {
                        "request_id": str(uuid4()),
                        "expected_revision": revision,
                        "rules_version": version,
                        "price_version": price_version,
                        "command": {"kind": "buy_packs", "set_id": "cel30", "count": 20},
                    }
                    started = time.perf_counter()
                    quote = client.post("/v1/quotes", headers=headers, json=body)
                    quoted = time.perf_counter()
                    assert quote.status_code == 200, quote.text
                    body["quoted_tokens"] = quote.json()["tokens"]
                    reply = client.post("/v1/commands", headers=headers, json=body)
                    ended = time.perf_counter()
                    assert reply.status_code == 200, reply.text
                    assert (
                        reply.json()["snapshot"]["state"]["packs"]["cel30"] == (revision + 1) * 20
                    )
                    if revision:
                        quotes.append((quoted - started) * 1000)
                        commits.append((ended - quoted) * 1000)
                        totals.append((ended - started) * 1000)
            return {
                "collection_card_kinds": collection_size,
                "packs_per_buy": 20,
                "cold_initialization_ms": round(cold_ms, 2),
                "quote": percentiles(quotes),
                "commit": percentiles(commits),
                "total": percentiles(totals),
                "worker_metrics": summary(),
            }
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resources-directory", type=Path, default=Path(__file__).resolve().parents[1] / "data"
    )
    parser.add_argument("--repetitions", type=int, default=30)
    args = parser.parse_args()
    if not 5 <= args.repetitions <= 100:
        parser.error("Use 5 to 100 repetitions")
    logging.getLogger("ppb.http").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for size in (0, 1000):
        print(json.dumps(benchmark(args.resources_directory, args.repetitions, size)), flush=True)
