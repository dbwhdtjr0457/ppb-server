# ppb-server

`uv`, FastAPI, SQLAlchemy, SQLite, Alembic으로 구성한 최소 API 서버입니다.

## 시작하기

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

서버 실행 후 다음 주소를 사용할 수 있습니다.

- API 문서: <http://127.0.0.1:8000/docs>
- 상태 확인: <http://127.0.0.1:8000/health>

## 예시

```bash
curl -X POST http://127.0.0.1:8000/items \
  -H 'Content-Type: application/json' \
  -d '{"name":"Sample item"}'

curl http://127.0.0.1:8000/items
```

## 개발 명령

```bash
uv run pytest
uv run ruff check .
```

