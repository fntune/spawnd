#!/bin/sh
set -eu

case "${1:-}" in
  *Username*)
    printf '%s\n' x-access-token
    ;;
  *)
    token="${SPAWND_GITHUB_TOKEN:-${GITHUB_TOKEN:-}}"
    if [ -z "$token" ]; then
      printf '%s\n' "SPAWND_GITHUB_TOKEN or GITHUB_TOKEN is required for git authentication" >&2
      exit 1
    fi
    printf '%s\n' "$token"
    ;;
esac
