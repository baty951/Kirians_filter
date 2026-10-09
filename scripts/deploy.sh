#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f .env ]]; then
    echo 'Create .env from .env.example before deploying.' >&2
    exit 1
fi
docker info >/dev/null

# Preserve installations created by the previous workflow's -p kirians-filter.
# An explicit environment/.env choice takes precedence over auto-detection.
if [[ -z "${COMPOSE_PROJECT_NAME:-}" ]] && ! grep -Eq '^[[:space:]]*COMPOSE_PROJECT_NAME[[:space:]]*=[[:space:]]*[^[:space:]#]+' .env; then
    current=0
    legacy=0
    if docker volume inspect kirians_filter_pgdata >/dev/null 2>&1; then current=1; fi
    if docker volume inspect kirians-filter_pgdata >/dev/null 2>&1; then legacy=1; fi
    if [[ "$current" == 1 && "$legacy" == 1 ]]; then
        echo 'Found both PostgreSQL volumes. Set COMPOSE_PROJECT_NAME in .env to the project with the active database.' >&2
        exit 1
    fi
    if [[ "$legacy" == 1 ]]; then
        export COMPOSE_PROJECT_NAME=kirians-filter
        printf '\nCOMPOSE_PROJECT_NAME=kirians-filter\n' >> .env
        echo 'Using existing kirians-filter PostgreSQL volume.'
    fi
fi

docker compose pull postgres redis
docker compose up -d --build --remove-orphans --wait --wait-timeout 180
docker image prune -f
