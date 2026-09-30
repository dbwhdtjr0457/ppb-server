"""Operational logs contain no account IDs, headers, query strings or bodies.

Account-scoped game events (including results and balance changes) live in the
same database transaction as the effect; these logs only describe HTTP health.
"""

import json
import logging
import time
from uuid import uuid4

logger = logging.getLogger("ppb.http")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False


async def record_requests(request, call_next):
    started = time.monotonic()
    correlation = str(uuid4())
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = correlation
        return response
    finally:
        route = request.scope.get("route")
        logger.info(
            json.dumps(
                {
                    "request_id": correlation,
                    "method": request.method,
                    "route": getattr(route, "path", "unmatched"),
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
        )
