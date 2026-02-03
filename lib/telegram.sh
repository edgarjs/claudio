#!/bin/bash

TELEGRAM_API="https://api.telegram.org/bot"

telegram_api() {
    local method="$1"
    shift
    curl -s "${TELEGRAM_API}${TELEGRAM_BOT_TOKEN}/${method}" "$@"
}

telegram_send_message() {
    local chat_id="$1"
    local text="$2"

    # Telegram has a 4096 char limit per message
    local max_len=4096
    while [ ${#text} -gt 0 ]; do
        local chunk="${text:0:$max_len}"
        text="${text:$max_len}"
        local result
        result=$(telegram_api "sendMessage" \
            -d "chat_id=${chat_id}" \
            --data-urlencode "text=${chunk}" \
            -d "parse_mode=Markdown")
        # If markdown fails, retry without parse_mode
        local ok
        ok=$(echo "$result" | jq -r '.ok // empty')
        if [ "$ok" != "true" ]; then
            telegram_api "sendMessage" \
                -d "chat_id=${chat_id}" \
                --data-urlencode "text=${chunk}" > /dev/null 2>&1
        fi
    done
}

telegram_send_typing() {
    local chat_id="$1"
    telegram_api "sendChatAction" \
        -d "chat_id=${chat_id}" \
        -d "action=typing" > /dev/null 2>&1
}

telegram_parse_webhook() {
    local body="$1"
    WEBHOOK_CHAT_ID=$(echo "$body" | jq -r '.message.chat.id // empty')
    WEBHOOK_TEXT=$(echo "$body" | jq -r '.message.text // empty')
    WEBHOOK_FROM_ID=$(echo "$body" | jq -r '.message.from.id // empty')
}

telegram_handle_webhook() {
    local body="$1"
    telegram_parse_webhook "$body"

    if [ -z "$WEBHOOK_CHAT_ID" ] || [ -z "$WEBHOOK_TEXT" ]; then
        return
    fi

    # Security: only allow configured chat_id
    if [ -n "$TELEGRAM_CHAT_ID" ] && [ "$WEBHOOK_CHAT_ID" != "$TELEGRAM_CHAT_ID" ]; then
        log "Rejected message from unauthorized chat_id: $WEBHOOK_CHAT_ID"
        return
    fi

    local text="$WEBHOOK_TEXT"

    case "$text" in
        /opus)
            MODEL="opus"
            claudio_save_env
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Switched to Opus model._"
            return
            ;;
        /sonnet)
            MODEL="sonnet"
            claudio_save_env
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Switched to Sonnet model._"
            return
            ;;
        /haiku)
            MODEL="haiku"
            claudio_save_env
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Switched to Haiku model._"
            return
            ;;
        /start)
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Hola!_ Send me a message and I'll forward it to Claude Code."
            return
            ;;
    esac

    log "Received message from chat_id=$WEBHOOK_CHAT_ID: $text"

    history_add "user" "$text"

    # Send typing indicator in a loop until claude finishes
    (while true; do telegram_send_typing "$WEBHOOK_CHAT_ID"; sleep 4; done) &
    local typing_pid=$!

    local response
    response=$(claude_run "$text")

    kill "$typing_pid" 2>/dev/null
    wait "$typing_pid" 2>/dev/null

    if [ -n "$response" ]; then
        history_add "assistant" "$response"
        telegram_send_message "$WEBHOOK_CHAT_ID" "$response"
    else
        telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't get a response. Please try again."
    fi
}

telegram_setup() {
    echo "=== Claudio Telegram Setup ==="
    echo ""

    read -rp "Enter your Telegram Bot Token: " token
    if [ -z "$token" ]; then
        echo "Error: Token cannot be empty."
        exit 1
    fi

    TELEGRAM_BOT_TOKEN="$token"

    local me
    me=$(telegram_api "getMe")
    local ok
    ok=$(echo "$me" | jq -r '.ok')
    if [ "$ok" != "true" ]; then
        echo "Error: Invalid bot token."
        exit 1
    fi
    local bot_name
    bot_name=$(echo "$me" | jq -r '.result.username')
    local bot_url="https://t.me/${bot_name}"
    echo "Bot verified: @${bot_name}"
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
            echo "Timed out waiting for /start message. Please try again."
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

    echo "Received /start from chat_id: ${TELEGRAM_CHAT_ID}"
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
        result=$(telegram_api "setWebhook" -d "url=${wh_url}")
        local wh_ok
        wh_ok=$(echo "$result" | jq -r '.ok')
        if [ "$wh_ok" != "true" ]; then
            echo "Warning: Failed to set webhook. It will be set on next service restart."
        else
            echo "Webhook set to: ${wh_url}"
        fi
    fi

    claudio_save_env
    echo ""
    echo "Setup complete! Restarting service..."
    service_restart 2>/dev/null || echo "Service not installed yet. Run 'claudio install' to set up the service."
}
