#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

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
    if [ -z "$TUNNEL_TYPE" ]; then
        log "server" "No tunnel configured. Skipping cloudflared."
        return
    fi

    # Temp file for parsing cloudflared output to detect tunnel URL
    local cf_log="$CLAUDIO_PATH/cloudflared.tmp"

    # Configurable timing (for testing)
    local poll_interval="${CLOUDFLARED_POLL_INTERVAL:-1}"
    local retry_delay="${CLOUDFLARED_RETRY_DELAY:-2}"
    local max_attempts="${CLOUDFLARED_MAX_ATTEMPTS:-10}"

    if [ "$TUNNEL_TYPE" = "ephemeral" ]; then
        cloudflared tunnel --url "http://localhost:${PORT}" > "$cf_log" 2>&1 &
        CLOUDFLARED_PID=$!
        trap 'kill $CLOUDFLARED_PID 2>/dev/null' EXIT

        # Wait for the ephemeral URL to appear in the log (3 retries)
        log "cloudflared" "Waiting for ephemeral tunnel URL..."
        local tunnel_url=""
        local retry=0
        local max_retries=3

        while [ $retry -lt $max_retries ]; do
            local attempts=0
            while [ $attempts -lt $max_attempts ]; do
                tunnel_url=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$cf_log" 2>/dev/null | head -1)
                if [ -n "$tunnel_url" ]; then
                    break 2
                fi
                sleep "$poll_interval"
                attempts=$((attempts + 1))
            done

            retry=$((retry + 1))
            if [ $retry -lt $max_retries ]; then
                log "cloudflared" "Tunnel URL not detected, retrying ($retry/$max_retries)..."
                kill $CLOUDFLARED_PID 2>/dev/null
                sleep "$retry_delay"
                cloudflared tunnel --url "http://localhost:${PORT}" > "$cf_log" 2>&1 &
                CLOUDFLARED_PID=$!
            fi
        done

        if [ -z "$tunnel_url" ]; then
            log_error "cloudflared" "Failed to detect ephemeral tunnel URL after $max_retries attempts."
            log "cloudflared" "Check $cf_log for details."
            kill $CLOUDFLARED_PID 2>/dev/null
            exit 1
        fi

        log "cloudflared" "Ephemeral tunnel URL: ${tunnel_url}"
        # shellcheck disable=SC2034  # Used by claudio_save_env
        WEBHOOK_URL="$tunnel_url"
        claudio_save_env

        # Auto-register webhook if Telegram is configured
        if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
            log "telegram" "Registering webhook at ${tunnel_url}/telegram/webhook..."
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
            else
                local error_desc
                error_desc=$(echo "$result" | jq -r '.description // "Unknown error"')
                log_error "telegram" "Webhook registration failed: ${error_desc}"
            fi
        fi

    elif [ "$TUNNEL_TYPE" = "named" ]; then
        cloudflared tunnel run --url "http://localhost:${PORT}" "$TUNNEL_NAME" > "$cf_log" 2>&1 &
        CLOUDFLARED_PID=$!
        trap 'kill $CLOUDFLARED_PID 2>/dev/null' EXIT
        log "cloudflared" "Named tunnel '$TUNNEL_NAME' started."
    fi
}

