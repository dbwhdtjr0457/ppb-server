#!/usr/bin/env python3
"""Run an installed macOS service without installing packages or migrating data."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

root = Path(os.environ["PPB_SERVICE_ROOT"]).resolve()
load_dotenv(root / "server.env", override=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.umask(0o077)
logs = root / "logs"
logs.mkdir(parents=True, exist_ok=True, mode=0o700)
handler = RotatingFileHandler(logs / "server.log", maxBytes=10 * 1024 * 1024, backupCount=5)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

import uvicorn  # noqa: E402

uvicorn.run(
    "app.main:app",
    host="127.0.0.1",
    port=int(os.getenv("PPB_PORT", "8000")),
    workers=1,
    access_log=False,
    proxy_headers=False,
    log_config=None,
)
