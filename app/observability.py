"""Operational logs contain no account IDs, headers, query strings or bodies.

Account-scoped game events (including results and balance changes) live in the
same database transaction as the effect; these logs only describe HTTP health.

Every request carries one request ID. The client may send it as X-Request-ID
(an opaque token, nothing else is accepted); otherwise one is generated. It is
returned on every response, including unhandled 500s, and an unhandled error is
logged with its traceback under the same ID. A report from the app can then be
matched to the exact server failure.

Logs go to stderr. Set PPB_LOG_DIRECTORY to also keep a rotating file there.
"""

import json
import logging
import os
import re
import time
import traceback
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse

logger = logging.getLogger("ppb.http")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
    log_directory = os.getenv("PPB_LOG_DIRECTORY", "")
    if log_directory:
        os.makedirs(log_directory, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_directory, "ppb-server.log"),
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(file_handler)
logger.propagate = False

current_request_id: ContextVar[str] = ContextVar("current_request_id", default="")
# Only an opaque token is trusted from the client; anything else is replaced.
_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def request_id_for(request) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    return supplied if _CLIENT_REQUEST_ID.fullmatch(supplied) else str(uuid4())


def error_trace(error):
    """Keep diagnostic locations, never exception text/SQL parameters or locals."""
    if error is None:
        return None
    entries, seen = [], set()
    while error is not None and id(error) not in seen and len(entries) < 8:
        seen.add(id(error))
        entries.append(
            {
                "type": type(error).__name__,
                "frames": [
                    {
                        "file": Path(frame.filename).name,
                        "line": frame.lineno,
                        "function": frame.name,
                    }
                    for frame in traceback.extract_tb(error.__traceback__)[-20:]
                ],
            }
        )
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return entries


async def record_requests(request, call_next):
    started = time.monotonic()
    correlation = request_id_for(request)
    token = current_request_id.set(correlation)
    status = 500
    try:
        try:
            response = await call_next(request)
        except Exception as error:  # noqa: BLE001 - logged with traceback, answered as JSON
            route = request.scope.get("route")
            logger.error(
                json.dumps(
                    {
                        "request_id": correlation,
                        "method": request.method,
                        "route": getattr(route, "path", "unmatched"),
                        "error": type(error).__name__,
                        "traceback": error_trace(error),
                    }
                ),
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "internal_error", "request_id": correlation},
            )
        status = response.status_code
        response.headers["X-Request-ID"] = correlation
        return response
    finally:
        route = request.scope.get("route")
        record = json.dumps(
            {
                "request_id": correlation,
                "method": request.method,
                "route": getattr(route, "path", "unmatched"),
                "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
        if status >= 500:
            logger.warning(record)
        else:
            logger.info(record)
        current_request_id.reset(token)


async def server_error(request, error):
    """Log a deliberate 5xx (rules engine, database retry) with its cause."""
    if error.status_code >= 500:
        route = request.scope.get("route")
        cause = error.__cause__
        logger.error(
            json.dumps(
                {
                    "request_id": current_request_id.get(),
                    "method": request.method,
                    "route": getattr(route, "path", "unmatched"),
                    "status": error.status_code,
                    "detail": str(error.detail),
                    "cause": type(cause).__name__ if cause else None,
                    "traceback": error_trace(cause),
                }
            ),
        )
    return await http_exception_handler(request, error)


def database_locked(error) -> bool:
    """SQLite's writer-lock timeout. Only the driver message is inspected, never logged."""
    original = getattr(error, "orig", None) or error
    return "locked" in str(original).lower()


async def database_error(request, error):
    """Answer a writer-lock timeout as a retryable 503 instead of a bare 500.

    Game commands already turn `OperationalError` into 503 retry codes, but reads that
    take the writer lock (stats, expiry during listings or trades, profile provisioning)
    failed with 500 once another writer held SQLite for longer than `busy_timeout`.
    """
    route = request.scope.get("route")
    correlation = current_request_id.get()
    locked = database_locked(error)
    record = {
        "request_id": correlation,
        "method": request.method,
        "route": getattr(route, "path", "unmatched"),
        "error": type(error).__name__,
        "traceback": error_trace(error),
    }
    if locked:
        logger.warning(json.dumps({**record, "detail": "database_busy_retry"}))
        return JSONResponse(
            status_code=503,
            content={"detail": "database_busy_retry", "request_id": correlation},
            headers={"Retry-After": "2"},
        )
    logger.error(json.dumps(record))
    return JSONResponse(
        status_code=500, content={"detail": "internal_error", "request_id": correlation}
    )
