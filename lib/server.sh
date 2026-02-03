#!/bin/bash

server_start() {
    log "Starting Claudio server on port ${PORT}..."

    # Start cloudflared tunnel in background
    cloudflared_start

    # Start HTTP server (blocks)
    local server_py
    server_py="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/server.py"
    PORT="$PORT" python3 "$server_py"
}

cloudflared_start() {
    if [ -z "$TUNNEL_TYPE" ]; then
        log "No tunnel configured. Skipping cloudflared."
        return
    fi

    local cf_log="$CLAUDIO_PATH/cloudflared.log"

    if [ "$TUNNEL_TYPE" = "ephemeral" ]; then
        cloudflared tunnel --url "http://localhost:${PORT}" > "$cf_log" 2>&1 &
        CLOUDFLARED_PID=$!
        trap 'kill $CLOUDFLARED_PID 2>/dev/null' EXIT

        # Wait for the ephemeral URL to appear in the log
        log "Waiting for ephemeral tunnel URL..."
        local attempts=0
        local tunnel_url=""
        while [ $attempts -lt 30 ]; do
            tunnel_url=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$cf_log" 2>/dev/null | head -1)
            if [ -n "$tunnel_url" ]; then
                break
            fi
            sleep 1
            attempts=$((attempts + 1))
        done

        if [ -z "$tunnel_url" ]; then
            log "Warning: Could not detect ephemeral tunnel URL."
            return
        fi

        log "Ephemeral tunnel URL: ${tunnel_url}"
        WEBHOOK_URL="$tunnel_url"
        claudio_save_env

        # Auto-register webhook if Telegram is configured
        if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
            log "Registering Telegram webhook at ${tunnel_url}/telegram/webhook..."
            local result
            result=$(curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
                -d "url=${tunnel_url}/telegram/webhook")
            log "Webhook registration: ${result}"
        fi

    elif [ "$TUNNEL_TYPE" = "named" ]; then
        cloudflared tunnel run --url "http://localhost:${PORT}" "$TUNNEL_NAME" > "$cf_log" 2>&1 &
        CLOUDFLARED_PID=$!
        trap 'kill $CLOUDFLARED_PID 2>/dev/null' EXIT
        log "Named tunnel '$TUNNEL_NAME' started."
    fi
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$CLAUDIO_LOG_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}
