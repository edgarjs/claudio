#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

TELEGRAM_API="https://api.telegram.org/bot"

telegram_api() {
    local method="$1"
    shift

    local max_retries=4
    local delays=(1 2 3 4)  # Total: 10 seconds max
    local attempt=0
    local response http_code body

    while [ $attempt -le $max_retries ]; do
        response=$(curl -s -w "\n%{http_code}" "${TELEGRAM_API}${TELEGRAM_BOT_TOKEN}/${method}" "$@")
        http_code=$(echo "$response" | tail -n1)
        body=$(echo "$response" | sed '$d')

        # Success or client error (4xx except 429) - don't retry
        if [[ "$http_code" =~ ^2 ]] || { [[ "$http_code" =~ ^4 ]] && [ "$http_code" != "429" ]; }; then
            echo "$body"
            return 0
        fi

        # Retryable: 429 (rate limit) or 5xx (server error)
        if [ $attempt -lt $max_retries ]; then
            local delay=${delays[$attempt]}
            log "telegram" "API error (HTTP $http_code), retrying in ${delay}s..."
            sleep "$delay"
        fi

        ((attempt++))
    done

    # All retries exhausted
    log_error "telegram" "API failed after $max_retries retries (HTTP $http_code)"
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
        # If markdown fails, retry without parse_mode
        local ok
        ok=$(echo "$result" | jq -r '.ok // empty')
        if [ "$ok" != "true" ]; then
            # Rebuild args without parse_mode
            args=(-d "chat_id=${chat_id}" --data-urlencode "text=${chunk}")
            if [ "$should_reply" = true ]; then
                args+=(-d "reply_to_message_id=${reply_to_message_id}")
            fi
            telegram_api "sendMessage" "${args[@]}" > /dev/null 2>&1
        fi
    done
}

telegram_send_typing() {
    local chat_id="$1"
    if ! telegram_api "sendChatAction" \
        -d "chat_id=${chat_id}" \
        -d "action=typing" > /dev/null; then
        log_error "telegram" "Failed to send typing indicator to chat $chat_id"
    fi
}

telegram_parse_webhook() {
    local body="$1"
    # Use printf instead of echo to safely handle untrusted data
    # (echo could misinterpret data starting with -e, -n, etc.)
    # Extract all values in a single jq call for efficiency
    local parsed
    parsed=$(printf '%s' "$body" | jq -r '[
        .message.chat.id // "",
        .message.message_id // "",
        .message.text // "",
        .message.from.id // "",
        .message.reply_to_message.text // "",
        .message.reply_to_message.from.first_name // ""
    ] | @tsv')

    # shellcheck disable=SC2034  # WEBHOOK_FROM_ID available for future use
    IFS=$'\t' read -r WEBHOOK_CHAT_ID WEBHOOK_MESSAGE_ID WEBHOOK_TEXT \
        WEBHOOK_FROM_ID WEBHOOK_REPLY_TO_TEXT WEBHOOK_REPLY_TO_FROM <<< "$parsed"
}

telegram_handle_webhook() {
    local body="$1"
    telegram_parse_webhook "$body"

    if [ -z "$WEBHOOK_CHAT_ID" ] || [ -z "$WEBHOOK_TEXT" ]; then
        return
    fi

    # Security: only allow configured chat_id
    if [ -n "$TELEGRAM_CHAT_ID" ] && [ "$WEBHOOK_CHAT_ID" != "$TELEGRAM_CHAT_ID" ]; then
        log "telegram" "Rejected message from unauthorized chat_id: $WEBHOOK_CHAT_ID"
        return
    fi

    local text="$WEBHOOK_TEXT"
    local message_id="$WEBHOOK_MESSAGE_ID"

    # If this is a reply, prepend the original message as context
    if [ -n "$WEBHOOK_REPLY_TO_TEXT" ]; then
        local reply_from="${WEBHOOK_REPLY_TO_FROM:-someone}"
        text="[Replying to ${reply_from}: \"${WEBHOOK_REPLY_TO_TEXT}\"]

${text}"
    fi

    case "$text" in
        /opus)
            MODEL="opus"
            claudio_save_env
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Switched to Opus model._" "$message_id"
            return
            ;;
        /sonnet)
            MODEL="sonnet"
            claudio_save_env
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Switched to Sonnet model._" "$message_id"
            return
            ;;
        /haiku)
            # shellcheck disable=SC2034  # Used by claude.sh via config
            MODEL="haiku"
            claudio_save_env
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Switched to Haiku model._" "$message_id"
            return
            ;;
        /start)
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Hola!_ Send me a message and I'll forward it to Claude Code." "$message_id"
            return
            ;;
    esac

    log "telegram" "Received message from chat_id=$WEBHOOK_CHAT_ID: $text"

    history_add "user" "$text"

    # Send typing indicator in a loop until claude finishes
    (while true; do telegram_send_typing "$WEBHOOK_CHAT_ID"; sleep 4; done) &
    local typing_pid=$!

    local response
    response=$(claude_run "$text")

    kill "$typing_pid" 2>/dev/null || true
    wait "$typing_pid" 2>/dev/null || true

    if [ -n "$response" ]; then
        history_add "assistant" "$response"
        telegram_send_message "$WEBHOOK_CHAT_ID" "$response" "$message_id"
    else
        telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't get a response. Please try again." "$message_id"
    fi
}

telegram_setup() {
    echo "=== Claudio Telegram Setup ==="
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
    telegram_send_message "$TELEGRAM_CHAT_ID" "_Hola!_ Send me a message and I'll forward it to Claude Code."

    # Re-register webhook
    local wh_url=""
    if [ "$TUNNEL_TYPE" = "named" ] && [ -n "$WEBHOOK_URL" ]; then
        wh_url="${WEBHOOK_URL}/telegram/webhook"
    elif [ "$TUNNEL_TYPE" = "ephemeral" ] && [ -n "$WEBHOOK_URL" ]; then
        wh_url="${WEBHOOK_URL}/telegram/webhook"
    fi

    if [ -n "$wh_url" ]; then
        local result
        local webhook_args=("-d" "url=${wh_url}")
        if [ -n "$WEBHOOK_SECRET" ]; then
            webhook_args+=("-d" "secret_token=${WEBHOOK_SECRET}")
        fi
        result=$(telegram_api "setWebhook" "${webhook_args[@]}")
        local wh_ok
        wh_ok=$(echo "$result" | jq -r '.ok')
        if [ "$wh_ok" != "true" ]; then
            local error_desc
            error_desc=$(echo "$result" | jq -r '.description // "Unknown error"')
            print_warning "Failed to set webhook: ${error_desc}"
            echo "It will be set on next service restart."
        else
            print_success "Webhook set to: ${wh_url}"
        fi
    fi

    claudio_save_env
    echo ""
    print_success "Setup complete! Restarting service..."
    service_restart 2>/dev/null || print_warning "Service not installed yet. Run 'claudio install' to set up the service."
}
