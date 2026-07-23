# Production image for the Behavioral Funnel Agent.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl for the healthcheck; postgresql-client so the entrypoint can apply schema.sql.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl postgresql-client \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh \
 && useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# The host (Railway) injects $PORT; fall back to 8000 locally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s \
  CMD curl -fsS http://127.0.0.1:${PORT:-8000}/health || exit 1

ENTRYPOINT ["sh", "entrypoint.sh"]
