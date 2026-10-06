import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.observability import logger, record_requests


def build_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(record_requests)

    @app.get("/ok")
    def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("database exploded")

    return app


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def capture() -> Capture:
    handler = Capture()
    logger.addHandler(handler)
    return handler


def test_client_request_id_is_echoed() -> None:
    client = TestClient(build_app())

    response = client.get("/ok", headers={"X-Request-ID": "abcd1234-ef56"})

    assert response.headers["X-Request-ID"] == "abcd1234-ef56"


def test_unusable_request_id_is_replaced() -> None:
    client = TestClient(build_app())

    response = client.get("/ok", headers={"X-Request-ID": "bad id\nwith newline"})

    assert response.headers["X-Request-ID"] != "bad id\nwith newline"
    assert len(response.headers["X-Request-ID"]) == 36


def test_unhandled_error_returns_request_id_and_logs_traceback() -> None:
    handler = capture()
    try:
        client = TestClient(build_app())

        response = client.get("/boom", headers={"X-Request-ID": "trace-0001"})
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 500
    assert response.json() == {"detail": "internal_error", "request_id": "trace-0001"}
    assert response.headers["X-Request-ID"] == "trace-0001"
    failures = [record for record in handler.records if record.levelno == logging.ERROR]
    assert len(failures) == 1
    assert "trace-0001" in failures[0].getMessage()
    assert '"route": "/boom"' in failures[0].getMessage()
    failure = json.loads(failures[0].getMessage())
    assert failure["traceback"][0]["type"] == "RuntimeError"
    assert any(frame["function"] == "boom" for frame in failure["traceback"][0]["frames"])
    assert failures[0].exc_info is None
    summaries = [record for record in handler.records if record.levelno == logging.WARNING]
    assert len(summaries) == 1
    assert '"status": 500' in summaries[0].getMessage()


def test_deliberate_server_error_logs_detail_and_cause() -> None:
    from fastapi import HTTPException
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.observability import server_error

    app = build_app()
    app.add_exception_handler(StarletteHTTPException, server_error)

    @app.get("/busy")
    def busy() -> dict[str, str]:
        try:
            raise TimeoutError("engine stalled")
        except TimeoutError as error:
            raise HTTPException(503, "rules_engine_timeout") from error

    @app.get("/missing")
    def missing() -> dict[str, str]:
        raise HTTPException(404, "not_found")

    handler = capture()
    try:
        client = TestClient(app)
        busy_response = client.get("/busy", headers={"X-Request-ID": "trace-0002"})
        missing_response = client.get("/missing")
    finally:
        logger.removeHandler(handler)

    assert busy_response.status_code == 503
    assert busy_response.json() == {"detail": "rules_engine_timeout"}
    assert busy_response.headers["X-Request-ID"] == "trace-0002"
    assert missing_response.status_code == 404
    failures = [record for record in handler.records if record.levelno == logging.ERROR]
    assert len(failures) == 1
    message = failures[0].getMessage()
    assert "trace-0002" in message
    assert "rules_engine_timeout" in message
    assert "TimeoutError" in message
    assert failures[0].exc_info is None
    assert json.loads(message)["traceback"][0]["type"] == "TimeoutError"


def test_database_error_trace_excludes_account_state_and_credentials():
    from sqlalchemy.exc import OperationalError

    app = build_app()
    private = "private-wallet-and-session-marker"

    @app.get("/database-error")
    def failure():
        raise OperationalError("UPDATE accounts SET state=?", (private,), RuntimeError(private))

    handler = capture()
    try:
        response = TestClient(app).get("/database-error")
    finally:
        logger.removeHandler(handler)
    assert response.status_code == 500
    logs = "\n".join(logging.Formatter().format(record) for record in handler.records)
    assert private not in logs and "UPDATE accounts" not in logs
    assert "OperationalError" in logs and '"function": "failure"' in logs


def test_request_id_with_final_newline_is_replaced():
    response = TestClient(build_app()).get("/ok", headers={"X-Request-ID": "trace-0001\n"})
    assert len(response.headers["X-Request-ID"]) == 36
