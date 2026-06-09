#!/usr/bin/env bash
set -euo pipefail

PROJECT="${SPAWND_PODMAN_PROJECT:-spawnd}"
NETWORK="${PROJECT}_default"
REMOVE_VOLUMES=0

if [[ "${1:-}" = "--volumes" ]]; then
  REMOVE_VOLUMES=1
fi

containers=(
  spawnd_worker_1
  spawnd_outbox_1
  spawnd_scheduler_1
  spawnd_submitter_1
  spawnd_api_1
  spawnd_migrate_1
  spawnd_codex-home-init_1
  spawnd_minio-init_1
  spawnd_minio_1
  spawnd_otel-collector_1
  spawnd_redis_1
  spawnd_postgres_1
)

for container in "${containers[@]}"; do
  podman rm -f "$container" >/dev/null 2>&1 || true
done

podman network rm "$NETWORK" >/dev/null 2>&1 || true

if [[ "$REMOVE_VOLUMES" = "1" ]]; then
  podman volume rm \
    "${PROJECT}_spawnd-postgres" \
    "${PROJECT}_spawnd-minio" \
    "${PROJECT}_spawnd-scratch" \
    "${PROJECT}_spawnd-codex-home" >/dev/null 2>&1 || true
fi
