#!/bin/sh
# Container entrypoint: wait for the DB, apply the (idempotent) schema, run the app.
set -e

if [ -n "$DATABASE_URL" ]; then
  echo "[entrypoint] waiting for database..."
  i=0
  until psql "$DATABASE_URL" -c 'SELECT 1' >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then
      echo "[entrypoint] database not reachable after ~60s; continuing anyway"
      break
    fi
    sleep 2
  done

  if [ "${RUN_SCHEMA_ON_STARTUP:-true}" = "true" ]; then
    echo "[entrypoint] applying schema.sql (idempotent)..."
    psql "$DATABASE_URL" -f schema.sql || echo "[entrypoint] schema apply reported errors (see above)"
  fi
else
  echo "[entrypoint] DATABASE_URL not set — skipping schema step"
fi

echo "[entrypoint] starting uvicorn on port ${PORT:-8000}"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
