#!/bin/sh
# Container entrypoint: the app applies schema.sql and waits for the DB itself
# (see db.connection.ensure_schema), so this just binds the host-provided $PORT.
set -e
echo "[entrypoint] starting uvicorn on port ${PORT:-8000}"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
