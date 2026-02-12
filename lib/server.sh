#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

# Register Telegram webhook with retry logic for a single bot.
# Usage: register_webhook <tunnel_url> [bot_token] [webhook_secret] [chat_id]
# When called without bot params, uses globals ($TELEGRAM_BOT_TOKEN, etc.)
register_webhook() {
    local tunnel_url="$1"
    local bot_token="${2:-$TELEGRAM_BOT_TOKEN}"
    local bot_secret="${3:-$WEBHOOK_SECRET}"
    local bot_chat_id="${4:-$TELEGRAM_CHAT_ID}"
    local webhook_retry_delay="${WEBHOOK_RETRY_DELAY:-60}"
    local max_retries=10
    local attempt=0

    log "telegram" "Registering webhook at ${tunnel_url}/telegram/webhook..."

    while [ "$attempt" -lt "$max_retries" ]; do
        ((attempt++)) || true
        local result
        # Pass bot token via --config to avoid exposing it in process list (ps aux)
        # Use a temporary file instead of process substitution for better compatibility
        local curl_config
        curl_config=$(mktemp)
        printf 'url = "https://api.telegram.org/bot%s/setWebhook"\n' "$bot_token" > "$curl_config"
        local curl_args=("-s" "--config" "$curl_config" "-d" "url=${tunnel_url}/telegram/webhook" "-d" 'allowed_updates=["message"]')
        if [ -n "$bot_secret" ]; then
            curl_args+=("-d" "secret_token=${bot_secret}")
        fi
        result=$(curl "${curl_args[@]}")
        rm -f "$curl_config"
        local wh_ok
        wh_ok=$(echo "$result" | jq -r '.ok')

        if [ "$wh_ok" = "true" ]; then
            log "telegram" "Webhook registered successfully."
            print_success "Webhook registered successfully."
            # Notify user via Telegram
            if [ -n "$bot_chat_id" ]; then
                # Temporarily set TELEGRAM_BOT_TOKEN for telegram_send_message
                local _saved_token="$TELEGRAM_BOT_TOKEN"
                TELEGRAM_BOT_TOKEN="$bot_token"
                telegram_send_message "$bot_chat_id" "Webhook registered! We can chat now. What are you up to?"
                TELEGRAM_BOT_TOKEN="$_saved_token"
            fi
            return 0
        fi

        local error_desc
        error_desc=$(echo "$result" | jq -r '.description // "Unknown error"')

        log_warn "telegram" "Webhook registration failed (attempt ${attempt}/${max_retries}): ${error_desc}. Retrying in ${webhook_retry_delay}s..."

        # Countdown timer for interactive mode, simple sleep for daemon
        if [ -t 0 ]; then
            for ((i=webhook_retry_delay; i>0; i--)); do
                printf "\r Retrying in %ds... " "$i"
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

# Register webhooks for all configured bots.
# Usage: register_all_webhooks <tunnel_url>
register_all_webhooks() {
    local tunnel_url="$1"
    local bot_ids
    bot_ids=$(claudio_list_bots)

    if [ -z "$bot_ids" ]; then
        log_warn "telegram" "No bots configured, skipping webhook registration."
        return 0
    fi

    local bot_id
    while IFS= read -r bot_id; do
        [ -z "$bot_id" ] && continue
        local bot_dir="$CLAUDIO_PATH/bots/$bot_id"
        local bot_env="$bot_dir/bot.env"
        [ -f "$bot_env" ] || continue

        # Load bot config using safe loader (defense-in-depth against command injection)
        local bot_token bot_secret bot_chat_id
        eval "$(
            (
                # Unset variables to ensure we load them from the bot's env file
                unset TELEGRAM_BOT_TOKEN WEBHOOK_SECRET TELEGRAM_CHAT_ID
                # Source the safe loader and the bot's env file
                # shellcheck source=lib/config.sh
                source "$(dirname "${BASH_SOURCE[0]}")/config.sh"
                _safe_load_env "$bot_env"
                # Print the variables in a format that can be eval'd
                printf 'bot_token=%q\n' "${TELEGRAM_BOT_TOKEN:-}"
                printf 'bot_secret=%q\n' "${WEBHOOK_SECRET:-}"
                printf 'bot_chat_id=%q\n' "${TELEGRAM_CHAT_ID:-}"
            )
        )"

        if [ -z "$bot_token" ]; then
            log_warn "telegram" "Skipping bot '$bot_id': no token configured"
            continue
        fi

        echo "Registering webhook for bot '$bot_id'..."
        register_webhook "$tunnel_url" "$bot_token" "$bot_secret" "$bot_chat_id"
    done <<< "$bot_ids"
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
    # shellcheck disable=SC2034  # CLOUDFLARED_PID used by tests in server.bats teardown
    CLOUDFLARED_PID=$!
    log "cloudflared" "Named tunnel '$TUNNEL_NAME' started."
}
