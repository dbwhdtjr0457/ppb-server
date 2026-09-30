from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.account_api import router as account_router
from app.auth_api import router as auth_router
from app.database import get_db
from app.game_api import router as game_router
from app.insights_api import router as insights_router
from app.jobs import lifespan
from app.observability import record_requests
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
app.middleware("http")(record_requests)

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
