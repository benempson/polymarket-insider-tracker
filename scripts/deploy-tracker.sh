#!/usr/bin/env bash
# =============================================================================
# deploy-tracker.sh — Deploy the Polymarket Insider Tracker on the VM
#
# Simple stop/start deploy — no blue-green needed since this is a background
# daemon, not an HTTP API serving live traffic.
#
# Required env vars:
#   TRACKER_IMAGE    — Docker image to deploy (e.g. ghcr.io/.../polymarket-insider-tracker:main)
#   TRACKER_APP_DIR  — Absolute path to app directory on the VM
#   REDIS_CONTAINER  — Name of the shared Redis container on the VM
# =============================================================================
set -euo pipefail

IMAGE="${TRACKER_IMAGE:?TRACKER_IMAGE env var required}"
APP_DIR="${TRACKER_APP_DIR:?TRACKER_APP_DIR env var required}"
REDIS_CONTAINER="${REDIS_CONTAINER:?REDIS_CONTAINER env var required}"
COMPOSE_FILE="$APP_DIR/docker-compose.prod.yml"
ENV_FILE="$APP_DIR/vars/.env.production"

HEALTH_RETRIES=24   # 24 x 5s = 120s
HEALTH_WAIT=5
HEALTH_URL="http://localhost:8085/health"

log() { echo "[deploy-tracker] $(date '+%H:%M:%S') $*"; }

# ---------------------------------------------------------------------------
# Discover the Docker network that the existing Redis container lives on
# ---------------------------------------------------------------------------
if docker inspect "$REDIS_CONTAINER" > /dev/null 2>&1; then
    REDIS_NETWORK=$(docker inspect "$REDIS_CONTAINER" --format='{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null | head -1)
    log "Found Redis container on network: $REDIS_NETWORK"
else
    log "WARNING: Redis container '$REDIS_CONTAINER' not found. Using bridge network."
    REDIS_NETWORK="bridge"
fi

# Source the POSTGRES_PASSWORD from the env file so compose can use it
if [ -f "$ENV_FILE" ]; then
    POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
    export POSTGRES_PASSWORD
fi

export TRACKER_IMAGE="$IMAGE"
export REDIS_CONTAINER
export REDIS_NETWORK
export TRACKER_APP_DIR="$APP_DIR"

log "Starting deploy: ${IMAGE}"

# ---------------------------------------------------------------------------
# Pull, stop, start
# ---------------------------------------------------------------------------
log "Pulling image ..."
docker pull "$IMAGE"

cd "$APP_DIR"

log "Stopping tracker ..."
docker compose -f "$COMPOSE_FILE" stop tracker 2>/dev/null || true
docker compose -f "$COMPOSE_FILE" rm -f tracker 2>/dev/null || true

log "Starting tracker (postgres will be created/kept as needed) ..."
docker compose -f "$COMPOSE_FILE" up -d

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
log "Waiting for health check (max $((HEALTH_RETRIES * HEALTH_WAIT))s) ..."
PASSED=0
for i in $(seq 1 "$HEALTH_RETRIES"); do
    sleep "$HEALTH_WAIT"
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        log "Health check passed (attempt $i)"
        PASSED=1
        break
    fi
    log "Health check attempt $i/$HEALTH_RETRIES ..."
done

if [ "$PASSED" -ne 1 ]; then
    log "ERROR: Tracker failed health check after ${HEALTH_RETRIES} attempts"
    log "--- Tracker logs (last 50 lines) ---"
    docker logs --tail=50 polymarket-tracker 2>&1 || true
    log "--- End of logs ---"
    exit 1
fi

log "Deploy complete."
