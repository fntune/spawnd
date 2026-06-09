#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PROJECT="${SPAWND_PODMAN_PROJECT:-spawnd}"
NETWORK="${PROJECT}_default"
POSTGRES_VOLUME="${PROJECT}_spawnd-postgres"
MINIO_VOLUME="${PROJECT}_spawnd-minio"
SCRATCH_VOLUME="${PROJECT}_spawnd-scratch"

APP_IMAGE="${SPAWND_APP_IMAGE:-localhost/spawnd:latest}"
ENV_FILE="${SPAWND_ENV_FILE:-$ROOT/.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

POSTGRES_IMAGE="${SPAWND_POSTGRES_IMAGE:-docker.io/library/postgres:16}"
REDIS_IMAGE="${SPAWND_REDIS_IMAGE:-docker.io/library/redis:7}"
MINIO_IMAGE="${SPAWND_MINIO_IMAGE:-docker.io/minio/minio:RELEASE.2025-04-22T22-12-26Z}"
MINIO_CLIENT_IMAGE="${SPAWND_MINIO_CLIENT_IMAGE:-docker.io/minio/mc:RELEASE.2025-04-16T18-13-26Z}"
OTEL_IMAGE="${SPAWND_OTEL_IMAGE:-docker.io/otel/opentelemetry-collector-contrib:0.138.0}"

DATABASE_URL="${SPAWND_DATABASE_URL:-postgresql+psycopg://spawnd:spawnd@postgres:5432/spawnd}"
REDIS_URL="${SPAWND_REDIS_URL:-redis://redis:6379/0}"
API_TOKEN="${SPAWND_API_TOKEN:-dev-token}"
GITHUB_WEBHOOK_SECRET="${SPAWND_GITHUB_WEBHOOK_SECRET:-dev-webhook-secret}"
ARTIFACTS_BUCKET="${SPAWND_ARTIFACTS_BUCKET:-spawnd-artifacts}"
ARTIFACTS_PREFIX="${SPAWND_ARTIFACTS_PREFIX:-dev}"
WORKER_ID="${SPAWND_WORKER_ID:-podman-worker-1}"
SUBMITTER_ID="${SPAWND_SUBMITTER_ID:-podman-submitter-1}"

if [[ -z "${SPAWND_CODEX_AUTH_DIR:-}" ]]; then
  echo "SPAWND_CODEX_AUTH_DIR must be set in $ENV_FILE or the environment" >&2
  exit 2
fi

if [[ ! -d "$SPAWND_CODEX_AUTH_DIR" ]]; then
  echo "SPAWND_CODEX_AUTH_DIR does not exist: $SPAWND_CODEX_AUTH_DIR" >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 2
fi

ENV_FILE_ARGS=(--env-file "$ENV_FILE")

app_env=(
  -e "SPAWND_DATABASE_URL=$DATABASE_URL"
  -e "SPAWND_REDIS_URL=$REDIS_URL"
  -e "SPAWND_ARTIFACTS_BUCKET=$ARTIFACTS_BUCKET"
  -e "SPAWND_ARTIFACTS_ENDPOINT=http://minio:9000"
  -e "SPAWND_ARTIFACTS_REGION=us-east-1"
  -e "SPAWND_ARTIFACTS_PREFIX=$ARTIFACTS_PREFIX"
)

worker_env=(
  "${app_env[@]}"
  -e "SPAWND_SCRATCH_ROOT=/scratch"
  -e "SPAWND_RUNTIME_ISOLATION=container"
  -e "SPAWND_TELEMETRY_ENABLED=1"
  -e "SPAWND_TELEMETRY_EXPORTER=otlp"
  -e "SPAWND_TELEMETRY_CAPTURE=orchestrator"
  -e "SPAWND_TELEMETRY_FAILURE_POLICY=degrade"
  -e "OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318"
  -e "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf"
  -e "GIT_AUTHOR_NAME=${GIT_AUTHOR_NAME:-spawnd}"
  -e "GIT_AUTHOR_EMAIL=${GIT_AUTHOR_EMAIL:-spawnd@example.invalid}"
  -e "GIT_COMMITTER_NAME=${GIT_COMMITTER_NAME:-spawnd}"
  -e "GIT_COMMITTER_EMAIL=${GIT_COMMITTER_EMAIL:-spawnd@example.invalid}"
  -e "GIT_TERMINAL_PROMPT=0"
  -e "SPAWND_GIT_ASKPASS=/usr/local/bin/spawnd-git-askpass"
  -e "GIT_ASKPASS=/usr/local/bin/spawnd-git-askpass"
  -e "AWS_ACCESS_KEY_ID=spawnd"
  -e "AWS_SECRET_ACCESS_KEY=spawnd-secret"
)

run_podman() {
  podman "$@"
}

ensure_network() {
  if ! run_podman network exists "$NETWORK" >/dev/null 2>&1; then
    run_podman network create "$NETWORK" >/dev/null
  fi
}

ensure_volume() {
  local name="$1"
  if ! run_podman volume exists "$name" >/dev/null 2>&1; then
    run_podman volume create "$name" >/dev/null
  fi
}

remove_container() {
  run_podman rm -f "$1" >/dev/null 2>&1 || true
}

wait_for() {
  local label="$1"
  local attempts="$2"
  shift 2

  for _ in $(seq 1 "$attempts"); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for $label" >&2
  return 1
}

build_image() {
  if [[ "${SPAWND_PODMAN_SKIP_BUILD:-0}" = "1" ]]; then
    return
  fi
  run_podman build -t "$APP_IMAGE" -f "$ROOT/Containerfile" "$ROOT"
}

start_infra() {
  remove_container spawnd_postgres_1
  run_podman run -d --replace --name spawnd_postgres_1 \
    --network "$NETWORK" --network-alias postgres \
    -e POSTGRES_DB=spawnd \
    -e POSTGRES_USER=spawnd \
    -e POSTGRES_PASSWORD=spawnd \
    -p 54329:5432 \
    -v "$POSTGRES_VOLUME:/var/lib/postgresql/data" \
    --health-cmd "pg_isready -U spawnd -d spawnd" \
    --health-interval 5s \
    --health-timeout 5s \
    --health-retries 20 \
    "$POSTGRES_IMAGE" >/dev/null

  remove_container spawnd_redis_1
  run_podman run -d --replace --name spawnd_redis_1 \
    --network "$NETWORK" --network-alias redis \
    -p 63799:6379 \
    --health-cmd "redis-cli ping" \
    --health-interval 5s \
    --health-timeout 5s \
    --health-retries 20 \
    "$REDIS_IMAGE" >/dev/null

  remove_container spawnd_otel-collector_1
  run_podman run -d --replace --name spawnd_otel-collector_1 \
    --network "$NETWORK" --network-alias otel-collector \
    -p 4317:4317 \
    -p 4318:4318 \
    -p 13133:13133 \
    -v "$ROOT/deploy/container/otel-collector.yml:/etc/otelcol/config.yml:ro" \
    "$OTEL_IMAGE" --config=/etc/otelcol/config.yml >/dev/null

  remove_container spawnd_minio_1
  run_podman run -d --replace --name spawnd_minio_1 \
    --network "$NETWORK" --network-alias minio \
    -e MINIO_ROOT_USER=spawnd \
    -e MINIO_ROOT_PASSWORD=spawnd-secret \
    -p 9000:9000 \
    -p 9001:9001 \
    -v "$MINIO_VOLUME:/data" \
    --health-cmd "curl -f http://localhost:9000/minio/health/live" \
    --health-interval 5s \
    --health-timeout 5s \
    --health-retries 20 \
    "$MINIO_IMAGE" server /data --console-address ":9001" >/dev/null

  wait_for postgres 60 run_podman exec spawnd_postgres_1 pg_isready -U spawnd -d spawnd
  wait_for redis 60 run_podman exec spawnd_redis_1 redis-cli ping
  wait_for minio 60 run_podman exec spawnd_minio_1 curl -fsS http://localhost:9000/minio/health/live
}

init_minio() {
  remove_container spawnd_minio-init_1
  run_podman run --rm --name spawnd_minio-init_1 \
    --network "$NETWORK" \
    --entrypoint /bin/sh \
    "$MINIO_CLIENT_IMAGE" -lc \
    "mc alias set local http://minio:9000 spawnd spawnd-secret && mc mb --ignore-existing local/$ARTIFACTS_BUCKET"
}

run_migrations() {
  remove_container spawnd_migrate_1
  run_podman run --rm --name spawnd_migrate_1 \
    --network "$NETWORK" \
    -e "SPAWND_DATABASE_URL=$DATABASE_URL" \
    "$APP_IMAGE" alembic upgrade head
}

start_processes() {
  remove_container spawnd_api_1
  run_podman run -d --replace --name spawnd_api_1 \
    --network "$NETWORK" --network-alias api \
    "${app_env[@]}" \
    -e "SPAWND_API_TOKEN=$API_TOKEN" \
    -e "SPAWND_GITHUB_WEBHOOK_SECRET=$GITHUB_WEBHOOK_SECRET" \
    -e "AWS_ACCESS_KEY_ID=spawnd" \
    -e "AWS_SECRET_ACCESS_KEY=spawnd-secret" \
    -p 8765:8765 \
    "$APP_IMAGE" spawnd serve --host 0.0.0.0 --port 8765 >/dev/null

  remove_container spawnd_submitter_1
  run_podman run -d --replace --name spawnd_submitter_1 \
    --network "$NETWORK" \
    "${app_env[@]}" \
    "$APP_IMAGE" spawnd submit-queue drain --poll --consumer-id "$SUBMITTER_ID" >/dev/null

  remove_container spawnd_scheduler_1
  run_podman run -d --replace --name spawnd_scheduler_1 \
    --network "$NETWORK" \
    "${app_env[@]}" \
    "$APP_IMAGE" spawnd schedules run-due --poll --idle-sleep-seconds 60 >/dev/null

  remove_container spawnd_outbox_1
  run_podman run -d --replace --name spawnd_outbox_1 \
    --network "$NETWORK" \
    "${app_env[@]}" \
    "$APP_IMAGE" spawnd drain-outbox --poll --idle-sleep-seconds 5 >/dev/null

  remove_container spawnd_worker_1
  run_podman run -d --replace --name spawnd_worker_1 \
    --network "$NETWORK" \
    "${ENV_FILE_ARGS[@]}" \
    "${worker_env[@]}" \
    -v "$SCRATCH_VOLUME:/scratch" \
    -v "$SPAWND_CODEX_AUTH_DIR:/root/.codex:ro" \
    "$APP_IMAGE" spawnd worker --poll --worker-id "$WORKER_ID" >/dev/null

  wait_for api 60 curl -fsS http://127.0.0.1:8765/readyz
}

remove_container spawnd_worker_1
remove_container spawnd_outbox_1
remove_container spawnd_scheduler_1
remove_container spawnd_submitter_1
remove_container spawnd_api_1
remove_container spawnd_migrate_1
remove_container spawnd_minio-init_1

ensure_network
ensure_volume "$POSTGRES_VOLUME"
ensure_volume "$MINIO_VOLUME"
ensure_volume "$SCRATCH_VOLUME"
build_image
start_infra
init_minio
run_migrations
start_processes

run_podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" | grep '^spawnd_' || true
echo "Spawnd Podman stack is ready at http://127.0.0.1:8765"
