#!/usr/bin/env bash
# =============================================================================
# watchdog.sh — Restart polymarket-tracker if it stops responding.
#
# Install via cron (runs every 5 minutes):
#   */5 * * * * /home/nueraadmin/polymarket-insider-tracker/scripts/watchdog.sh >> /var/log/tracker-watchdog.log 2>&1
#
# The Docker healthcheck handles most cases, but this catches scenarios
# where Docker itself doesn't act (e.g. the health status is stuck).
# =============================================================================
set -u

CONTAINER="polymarket-tracker"
HEALTH_URL="http://localhost:8085/health"
APP_DIR="${TRACKER_APP_DIR:-/home/nueraadmin/polymarket-insider-tracker}"
COMPOSE_FILE="$APP_DIR/docker-compose.prod.yml"

log() { echo "[watchdog] $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# Check if the container exists at all
if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
    log "Container $CONTAINER not found — skipping (not deployed yet?)"
    exit 0
fi

# Check health endpoint
if curl -sf --max-time 10 "$HEALTH_URL" > /dev/null 2>&1; then
    exit 0
fi

log "Health check failed — checking container status..."
STATUS=$(docker inspect "$CONTAINER" --format='{{.State.Status}}' 2>/dev/null)
HEALTH=$(docker inspect "$CONTAINER" --format='{{.State.Health.Status}}' 2>/dev/null)
log "Container status=$STATUS health=$HEALTH"

# If Docker already knows it's unhealthy, let Docker's restart policy handle it.
# But if it's been unhealthy for a while and Docker hasn't restarted, force it.
if [ "$STATUS" = "running" ]; then
    log "Container is running but not responding — restarting..."

    # Source env vars needed by compose
    if [ -f "$APP_DIR/vars/.env.production" ]; then
        POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' "$APP_DIR/vars/.env.production" | cut -d= -f2- | tr -d '\r')
        export POSTGRES_PASSWORD
    fi
    export TRACKER_IMAGE=$(docker inspect "$CONTAINER" --format='{{.Config.Image}}' 2>/dev/null)
    export REDIS_CONTAINER=$(docker inspect "$CONTAINER" --format='{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^REDIS_URL=' | sed 's|.*://\(.*\):.*|\1|')
    export REDIS_NETWORK=$(docker inspect "$REDIS_CONTAINER" --format='{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null | head -1)
    export TRACKER_APP_DIR="$APP_DIR"

    cd "$APP_DIR"
    docker compose -f "$COMPOSE_FILE" stop tracker
    docker compose -f "$COMPOSE_FILE" rm -f tracker
    docker compose -f "$COMPOSE_FILE" up -d tracker

    log "Restart complete."
else
    log "Container is not running (status=$STATUS) — Docker restart policy should handle this."
fi
