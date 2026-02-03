#!/bin/bash

# Webhook health check script - verifies and re-registers Telegram webhook if needed
# Intended to be run periodically via cron

set -euo pipefail

CLAUDIO_PATH="$HOME/.claudio"
CLAUDIO_ENV_FILE="$CLAUDIO_PATH/service.env"
LOG_FILE="$CLAUDIO_PATH/webhook-check.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# Load environment
if [ ! -f "$CLAUDIO_ENV_FILE" ]; then
    log "ERROR: Environment file not found: $CLAUDIO_ENV_FILE"
    exit 1
fi

set -a
source "$CLAUDIO_ENV_FILE"
set +a

# Validate required variables
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    log "ERROR: TELEGRAM_BOT_TOKEN not set"
    exit 1
fi

if [ -z "${WEBHOOK_URL:-}" ]; then
    log "ERROR: WEBHOOK_URL not set"
    exit 1
fi

EXPECTED_WEBHOOK_URL="${WEBHOOK_URL}/telegram/webhook"
TELEGRAM_API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

# Check current webhook status
response=$(curl -s "${TELEGRAM_API}/getWebhookInfo")
ok=$(echo "$response" | jq -r '.ok // false')

if [ "$ok" != "true" ]; then
    log "ERROR: Failed to get webhook info: $response"
    exit 1
fi

current_url=$(echo "$response" | jq -r '.result.url // empty')

if [ "$current_url" = "$EXPECTED_WEBHOOK_URL" ]; then
    # Webhook is correctly configured, nothing to do
    exit 0
fi

# Webhook needs to be re-registered
log "Webhook mismatch detected. Current: '${current_url}', Expected: '${EXPECTED_WEBHOOK_URL}'"

# Build webhook registration request
webhook_data="url=${EXPECTED_WEBHOOK_URL}"
if [ -n "${WEBHOOK_SECRET:-}" ]; then
    webhook_data="${webhook_data}&secret_token=${WEBHOOK_SECRET}"
fi

result=$(curl -s -X POST "${TELEGRAM_API}/setWebhook" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "$webhook_data" \
    -d "allowed_updates=[\"message\"]")

set_ok=$(echo "$result" | jq -r '.ok // false')

if [ "$set_ok" = "true" ]; then
    log "Webhook re-registered successfully: ${EXPECTED_WEBHOOK_URL}"
else
    log "ERROR: Failed to re-register webhook: $result"
    exit 1
fi
