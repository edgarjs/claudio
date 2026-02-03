#!/bin/bash

# Webhook health check script - calls /health endpoint which verifies and fixes webhook
# Intended to be run periodically via cron

set -euo pipefail

CLAUDIO_PATH="$HOME/.claudio"
CLAUDIO_ENV_FILE="$CLAUDIO_PATH/service.env"
LOG_FILE="$CLAUDIO_PATH/webhook-check.log"

log() {
    local msg
    msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg" >> "$LOG_FILE"
    if [[ "$*" == ERROR:* ]]; then
        echo "$msg" >&2
    fi
}

# Load environment for PORT
if [ ! -f "$CLAUDIO_ENV_FILE" ]; then
    log "ERROR: Environment file not found: $CLAUDIO_ENV_FILE"
    exit 1
fi

set -a
# shellcheck source=/dev/null
source "$CLAUDIO_ENV_FILE"
set +a

PORT="${PORT:-8421}"

# Call health endpoint - it will check and fix webhook if needed
response=$(curl -s -w "\n%{http_code}" "http://localhost:${PORT}/health" 2>/dev/null || echo -e "\n000")
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" = "200" ]; then
    # Healthy - nothing to log unless there are pending updates
    pending=$(echo "$body" | jq -r '.checks.telegram_webhook.pending_updates // 0' 2>/dev/null || echo "0")
    if [ "$pending" != "0" ] && [ "$pending" != "null" ]; then
        log "Health OK (pending updates: $pending)"
    fi
elif [ "$http_code" = "503" ]; then
    log "Health check returned unhealthy: $body"
    exit 1
elif [ "$http_code" = "000" ]; then
    log "ERROR: Could not connect to server on port $PORT"
    exit 1
else
    log "ERROR: Unexpected response (HTTP $http_code): $body"
    exit 1
fi
