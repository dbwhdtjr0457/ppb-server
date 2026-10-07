from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.account_api import router as account_router
from app.auth_api import router as auth_router
from app.auth_service import diagnostics_allowed
from app.config import settings
from app.database import get_db
from app.game_api import router as game_router
from app.insights_api import router as insights_router
from app.jobs import lifespan
from app.observability import database_error, record_requests, server_error
from app.online_api import router as online_router
from app.opening_jobs import router as opening_jobs_router
from app.operations_api import router as operations_router

app = FastAPI(title="ppb-server", lifespan=lifespan)
app.include_router(game_router)
app.include_router(auth_router)
app.include_router(account_router)
app.include_router(insights_router)
app.include_router(online_router)
app.include_router(operations_router)
app.include_router(opening_jobs_router)
# Every command and full state reply carries the whole account state (about 1.3 MB
# for a long-time player, mostly opening history), and the 7-8 MB price snapshot
# goes out on each price change. Over a home uplink and tunnel that dominated
# response time; JSON compresses to roughly 7% for a few milliseconds of CPU.
# Registered first so it sits innermost: the outer http middlewares stream bodies
# in chunks, which GZipMiddleware would treat as streaming and compress even tiny
# replies regardless of minimum_size.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.middleware("http")(record_requests)
app.add_exception_handler(StarletteHTTPException, server_error)
app.add_exception_handler(OperationalError, database_error)


@app.middleware("http")
async def private_diagnostics(request, call_next):
    if (
        settings.private_diagnostics
        and request.url.path
        in {"/ready", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
        and not diagnostics_allowed(request)
    ):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await call_next(request)


DbSession = Annotated[Session, Depends(get_db)]


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    # Pydantic's default detail includes the invalid input (possibly a password).
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {key: value for key, value in entry.items() if key in {"loc", "type", "msg"}}
                for entry in error.errors()
            ]
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready(db: DbSession):
    from app.game_service import initial_state
    from app.rules import rules

    try:
        revision = db.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != "20260930_0006":
            raise ValueError("Migration required")
        _, _, version = rules.apply(initial_state(), {"kind": "inspect"})
    except Exception as error:
        raise HTTPException(503, "database_migration_or_rules_not_ready") from error
    return {"status": "ready", "rules_version": version}
