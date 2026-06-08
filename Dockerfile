FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md alembic.ini /app/
COPY spawnd /app/spawnd

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[deployed,telemetry,openai,codex,sdk]"

CMD ["spawnd", "serve", "--host", "0.0.0.0", "--port", "8765"]
