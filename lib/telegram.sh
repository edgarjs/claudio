#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

TELEGRAM_API="https://api.telegram.org/bot"

telegram_api() {
    local method="$1"
    shift

    local max_retries=4
    local attempt=0
    local response http_code body

    while [ $attempt -le $max_retries ]; do
        # Pass bot token via --config to avoid exposing it in process list (ps aux)
        response=$(curl -s -w "\n%{http_code}" \
            --config <(printf 'url = "%s%s/%s"\n' "$TELEGRAM_API" "$TELEGRAM_BOT_TOKEN" "$method") \
            "$@")
        http_code=$(echo "$response" | tail -n1)
        body=$(echo "$response" | sed '$d')

        # Success or client error (4xx except 429) - don't retry
        if [[ "$http_code" =~ ^2 ]] || { [[ "$http_code" =~ ^4 ]] && [ "$http_code" != "429" ]; }; then
            echo "$body"
            return 0
        fi

        # Retryable: 429 (rate limit) or 5xx (server error)
        if [ $attempt -lt $max_retries ]; then
            local delay
            if [ "$http_code" = "429" ]; then
                # Use Telegram's retry_after if provided, otherwise exponential backoff
                delay=$(echo "$body" | jq -r '.parameters.retry_after // empty')
                if [ -z "$delay" ] || [ "$delay" -lt 1 ] 2>/dev/null; then
                    delay=$(( 2 ** attempt ))  # 1, 2, 4, 8
                fi
            else
                delay=$(( 2 ** attempt ))  # Exponential backoff for 5xx
            fi
            log "telegram" "API error (HTTP $http_code), retrying in ${delay}s..."
            sleep "$delay"
        fi

        ((attempt++)) || true
    done

    # All retries exhausted
    log_error "telegram" "API failed after $((max_retries + 1)) attempts (HTTP $http_code)"
    echo "$body"
    return 1
}

telegram_send_message() {
    local chat_id="$1"
    local text="$2"
    local reply_to_message_id="${3:-}"

    # Telegram has a 4096 char limit per message
    local max_len=4096
    local is_first=true
    while [ ${#text} -gt 0 ]; do
        local chunk="${text:0:$max_len}"
        text="${text:$max_len}"

        # Determine if this chunk should reply to the original message
        local should_reply=false
        if [ "$is_first" = true ] && [ -n "$reply_to_message_id" ]; then
            should_reply=true
        fi
        is_first=false

        # Build curl arguments
        local args=(-d "chat_id=${chat_id}" --data-urlencode "text=${chunk}" -d "parse_mode=Markdown")
        if [ "$should_reply" = true ]; then
            args+=(-d "reply_to_message_id=${reply_to_message_id}")
        fi

        local result
        result=$(telegram_api "sendMessage" "${args[@]}")
        # If send fails, retry with progressively fewer options
        local ok
        ok=$(echo "$result" | jq -r '.ok // empty' 2>/dev/null)
        if [ "$ok" != "true" ]; then
            # Retry without parse_mode (keeps reply_to)
            args=(-d "chat_id=${chat_id}" --data-urlencode "text=${chunk}")
            if [ "$should_reply" = true ]; then
                args+=(-d "reply_to_message_id=${reply_to_message_id}")
            fi
            result=$(telegram_api "sendMessage" "${args[@]}") || true
            ok=$(echo "$result" | jq -r '.ok // empty' 2>/dev/null)
            if [ "$ok" != "true" ]; then
                # Retry without reply_to (e.g. synthetic Alexa message_ids)
                args=(-d "chat_id=${chat_id}" --data-urlencode "text=${chunk}")
                result=$(telegram_api "sendMessage" "${args[@]}") || true
                ok=$(echo "$result" | jq -r '.ok // empty' 2>/dev/null)
                if [ "$ok" != "true" ]; then
                    log_error "telegram" "Failed to send message after all fallbacks for chat $chat_id"
                fi
            fi
        fi
    done
}

telegram_setup() {
    local bot_id="${1:-}"

    echo "=== Claudio Telegram Setup ==="
    if [ -n "$bot_id" ]; then
        echo "Bot: $bot_id"
    fi
    echo ""

    read -rp "Enter your Telegram Bot Token: " token
    if [ -z "$token" ]; then
        print_error "Token cannot be empty."
        exit 1
    fi

    TELEGRAM_BOT_TOKEN="$token"

    local me
    me=$(telegram_api "getMe")
    local ok
    ok=$(echo "$me" | jq -r '.ok')
    if [ "$ok" != "true" ]; then
        print_error "Invalid bot token."
        exit 1
    fi
    local bot_name
    bot_name=$(echo "$me" | jq -r '.result.username')
    local bot_url="https://t.me/${bot_name}"
    print_success "Bot verified: @${bot_name}"
    echo "Bot URL: ${bot_url}"

    # Remove webhook temporarily so getUpdates works for polling
    telegram_api "deleteWebhook" -d "drop_pending_updates=true" > /dev/null 2>&1

    echo ""
    echo "Opening ${bot_url} ..."
    echo "Send /start to your bot from the Telegram account you want to use."
    echo "Waiting for the message..."

    # Open bot URL in browser
    if [[ "$(uname)" == "Darwin" ]]; then
        open "$bot_url" 2>/dev/null
    else
        xdg-open "$bot_url" 2>/dev/null || true
    fi

    local timeout=120
    local start_time
    start_time=$(date +%s)

    while true; do
        local now
        now=$(date +%s)
        local elapsed=$(( now - start_time ))
        if [ "$elapsed" -ge "$timeout" ]; then
            print_error "Timed out waiting for /start message. Please try again."
            exit 1
        fi

        # Poll for updates using getUpdates
        local updates
        updates=$(telegram_api "getUpdates" -d "timeout=5" -d "allowed_updates=[\"message\"]")
        local msg_text msg_chat_id
        msg_text=$(echo "$updates" | jq -r '.result[-1].message.text // empty')
        msg_chat_id=$(echo "$updates" | jq -r '.result[-1].message.chat.id // empty')

        if [ "$msg_text" = "/start" ] && [ -n "$msg_chat_id" ]; then
            TELEGRAM_CHAT_ID="$msg_chat_id"
            # Clear updates
            local update_id
            update_id=$(echo "$updates" | jq -r '.result[-1].update_id')
            telegram_api "getUpdates" -d "offset=$((update_id + 1))" > /dev/null 2>&1
            break
        fi

        sleep 1
    done

    print_success "Received /start from chat_id: ${TELEGRAM_CHAT_ID}"
    telegram_send_message "$TELEGRAM_CHAT_ID" "Hola! Please return to your terminal to complete the webhook setup."

    # Verify tunnel is configured
    if [ -z "$WEBHOOK_URL" ]; then
        print_warning "No tunnel configured. Run 'claudio install' first."
        exit 1
    fi

    # Save config: per-bot or global
    if [ -n "$bot_id" ]; then
        # Validate bot_id format to prevent path traversal
        if [[ ! "$bot_id" =~ ^[a-zA-Z0-9_-]+$ ]]; then
            print_error "Invalid bot name: '$bot_id'. Use only letters, numbers, hyphens, and underscores."
            exit 1
        fi

        local bot_dir="$CLAUDIO_PATH/bots/$bot_id"
        mkdir -p "$bot_dir"
        chmod 700 "$bot_dir"

        # Load existing config to preserve other platform's credentials
        export CLAUDIO_BOT_ID="$bot_id"
        export CLAUDIO_BOT_DIR="$bot_dir"
        export CLAUDIO_DB_FILE="$bot_dir/history.db"
        # Unset OTHER platform's credentials to prevent stale values from leaking
        # (Don't unset Telegram vars - they were just set above!)
        unset WEBHOOK_SECRET WHATSAPP_PHONE_NUMBER_ID WHATSAPP_ACCESS_TOKEN \
            WHATSAPP_APP_SECRET WHATSAPP_VERIFY_TOKEN WHATSAPP_PHONE_NUMBER
        if [ -f "$bot_dir/bot.env" ]; then
            # shellcheck source=/dev/null
            source "$bot_dir/bot.env" 2>/dev/null || true
        fi

        # Re-apply new Telegram credentials (source may have overwritten them during re-configuration)
        export TELEGRAM_BOT_TOKEN="$token"
        export TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID"

        # Generate per-bot webhook secret (only if not already set)
        if [ -z "${WEBHOOK_SECRET:-}" ]; then
            export WEBHOOK_SECRET
            WEBHOOK_SECRET=$(openssl rand -hex 32) || {
                print_error "Failed to generate WEBHOOK_SECRET"
                exit 1
            }
        fi

        claudio_save_bot_env

        print_success "Bot config saved to $bot_dir/bot.env"
    else
        claudio_save_env

        # Restart service
        echo ""
        echo "Restarting service..."
        service_restart 2>/dev/null || {
            print_warning "Service not installed yet. Run 'claudio install' to set up the service."
            return
        }

        # Register webhook (will retry until successful)
        echo ""
        echo "Registering Telegram webhook (DNS propagation could take a moment)..."
        register_webhook "$WEBHOOK_URL"
    fi

    print_success "Setup complete!"
}
