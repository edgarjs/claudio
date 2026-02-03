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

        # Wait for the ephemeral URL to appear in the log (3 retries, 10s each)
        log "Waiting for ephemeral tunnel URL..."
        local tunnel_url=""
        local retry=0
        local max_retries=3

        while [ $retry -lt $max_retries ]; do
            local attempts=0
            while [ $attempts -lt 10 ]; do
                tunnel_url=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$cf_log" 2>/dev/null | head -1)
                if [ -n "$tunnel_url" ]; then
                    break 2
                fi
                sleep 1
                attempts=$((attempts + 1))
            done

            retry=$((retry + 1))
            if [ $retry -lt $max_retries ]; then
                log "Tunnel URL not detected, retrying ($retry/$max_retries)..."
                kill $CLOUDFLARED_PID 2>/dev/null
                sleep 2
                cloudflared tunnel --url "http://localhost:${PORT}" > "$cf_log" 2>&1 &
                CLOUDFLARED_PID=$!
            fi
        done

        if [ -z "$tunnel_url" ]; then
            log "ERROR: Failed to detect ephemeral tunnel URL after $max_retries attempts."
            log "Check $cf_log for details."
            kill $CLOUDFLARED_PID 2>/dev/null
            exit 1
        fi

        log "Ephemeral tunnel URL: ${tunnel_url}"
        WEBHOOK_URL="$tunnel_url"
        claudio_save_env

        # Auto-register webhook if Telegram is configured
        if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
            log "Registering Telegram webhook at ${tunnel_url}/telegram/webhook..."
            local result
            local curl_args=("-s" "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" "-d" "url=${tunnel_url}/telegram/webhook")
            if [ -n "$WEBHOOK_SECRET" ]; then
                curl_args+=("-d" "secret_token=${WEBHOOK_SECRET}")
            fi
            result=$(curl "${curl_args[@]}")
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
