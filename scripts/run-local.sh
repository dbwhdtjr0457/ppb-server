#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
if [[ ! -f .env ]]; then
  echo 'Create .env from .env.example and set PPB_RULES_EXECUTABLE first.' >&2
  exit 1
fi
uv sync --locked
uv run --env-file .env alembic upgrade head
# Loopback only; do not trust caller-supplied forwarding headers for rate limits.
exec uv run --env-file .env uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
