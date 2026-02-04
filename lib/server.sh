#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

# Register Telegram webhook with retry logic
# Usage: register_webhook <tunnel_url>
register_webhook() {
    local tunnel_url="$1"
    local webhook_retry_delay="${WEBHOOK_RETRY_DELAY:-60}"  # 1 minute between retries

    log "telegram" "Registering webhook at ${tunnel_url}/telegram/webhook..."

    while true; do
        local result
        local curl_args=("-s" "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" "-d" "url=${tunnel_url}/telegram/webhook")
        if [ -n "$WEBHOOK_SECRET" ]; then
            curl_args+=("-d" "secret_token=${WEBHOOK_SECRET}")
        fi
        result=$(curl "${curl_args[@]}")
        local wh_ok
        wh_ok=$(echo "$result" | jq -r '.ok')

        if [ "$wh_ok" = "true" ]; then
            log "telegram" "Webhook registered successfully."
            print_success "Webhook registered successfully."
            return 0
        fi

        local error_desc
        error_desc=$(echo "$result" | jq -r '.description // "Unknown error"')

        log_warn "telegram" "Webhook registration failed: ${error_desc}. Retrying in ${webhook_retry_delay}s..."

        # Countdown timer for interactive mode, simple sleep for daemon
        if [ -t 0 ]; then
            for ((i=webhook_retry_delay; i>0; i--)); do
                printf "\r⏳ Retrying in %ds... " "$i"
                sleep 1
            done
            printf "\r                       \r"  # Clear the line
        else
            sleep "$webhook_retry_delay"
        fi
    done
}

server_start() {
    log "server" "Starting Claudio server on port ${PORT}..."

    # Start cloudflared tunnel in background
    cloudflared_start

    # Start HTTP server (blocks)
    local server_py
    server_py="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/server.py"
    PORT="$PORT" python3 "$server_py"
}

cloudflared_start() {
    if [ -z "$TUNNEL_TYPE" ] || [ -z "$TUNNEL_NAME" ]; then
        log "server" "No tunnel configured. Skipping cloudflared."
        return
    fi

    local cf_log="$CLAUDIO_PATH/cloudflared.tmp"

    cloudflared tunnel run --url "http://localhost:${PORT}" "$TUNNEL_NAME" > "$cf_log" 2>&1 &
    CLOUDFLARED_PID=$!
    trap 'kill $CLOUDFLARED_PID 2>/dev/null' EXIT
    log "cloudflared" "Named tunnel '$TUNNEL_NAME' started."
}

