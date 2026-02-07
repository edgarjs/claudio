#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

# Register Telegram webhook with retry logic
# Usage: register_webhook <tunnel_url>
register_webhook() {
    local tunnel_url="$1"
    local webhook_retry_delay="${WEBHOOK_RETRY_DELAY:-60}"
    local max_retries=10
    local attempt=0

    log "telegram" "Registering webhook at ${tunnel_url}/telegram/webhook..."

    while [ "$attempt" -lt "$max_retries" ]; do
        ((attempt++)) || true
        local result
        # Pass bot token via --config to avoid exposing it in process list (ps aux)
        local curl_args=("-s" "--config" <(printf 'url = "https://api.telegram.org/bot%s/setWebhook"\n' "$TELEGRAM_BOT_TOKEN") "-d" "url=${tunnel_url}/telegram/webhook" "-d" 'allowed_updates=["message"]')
        if [ -n "$WEBHOOK_SECRET" ]; then
            curl_args+=("-d" "secret_token=${WEBHOOK_SECRET}")
        fi
        result=$(curl "${curl_args[@]}")
        local wh_ok
        wh_ok=$(echo "$result" | jq -r '.ok')

        if [ "$wh_ok" = "true" ]; then
            log "telegram" "Webhook registered successfully."
            print_success "Webhook registered successfully."
            # Notify user via Telegram
            if [ -n "$TELEGRAM_CHAT_ID" ]; then
                telegram_send_message "$TELEGRAM_CHAT_ID" "✅ Webhook registered! We can chat now. What are you up to?"
            fi
            return 0
        fi

        local error_desc
        error_desc=$(echo "$result" | jq -r '.description // "Unknown error"')

        log_warn "telegram" "Webhook registration failed (attempt ${attempt}/${max_retries}): ${error_desc}. Retrying in ${webhook_retry_delay}s..."

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

    log_error "telegram" "Webhook registration failed after ${max_retries} attempts."
    print_error "Webhook registration failed after ${max_retries} attempts."
    return 1
}

server_start() {
    log "server" "Starting Claudio server on port ${PORT}..."

    # Start HTTP server (exec replaces bash so SIGTERM reaches Python directly)
    # Python manages cloudflared lifecycle directly for proper cleanup
    local server_py
    server_py="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/server.py"
    exec env PORT="$PORT" python3 "$server_py"
}

cloudflared_start() {
    # Called only by tests — production uses Python-managed cloudflared in server.py
    if [ -z "$TUNNEL_NAME" ]; then
        log "server" "No tunnel configured. Skipping cloudflared."
        return
    fi

    local cf_log="$CLAUDIO_PATH/cloudflared.tmp"

    cloudflared tunnel run --url "http://localhost:${PORT}" "$TUNNEL_NAME" > "$cf_log" 2>&1 &
    CLOUDFLARED_PID=$!
    log "cloudflared" "Named tunnel '$TUNNEL_NAME' started."
}
