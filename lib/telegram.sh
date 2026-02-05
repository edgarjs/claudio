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

telegram_send_voice() {
    local chat_id="$1"
    local audio_file="$2"
    local reply_to_message_id="${3:-}"

    local args=(-F "chat_id=${chat_id}" -F "voice=@${audio_file}")
    if [ -n "$reply_to_message_id" ]; then
        args+=(-F "reply_to_message_id=${reply_to_message_id}")
    fi

    telegram_api "sendVoice" "${args[@]}"
}

telegram_send_typing() {
    local chat_id="$1"
    # Fire-and-forget: don't retry typing indicators to avoid rate limit cascades
    curl -s "${TELEGRAM_API}${TELEGRAM_BOT_TOKEN}/sendChatAction" \
        -d "chat_id=${chat_id}" \
        -d "action=typing" > /dev/null 2>&1 || true
}

# Progress messages to show while Claude is working
PROGRESS_MESSAGES=(
    "_Still working on it..._"
    "_This is taking a bit longer, hang tight..._"
    "_Still processing, almost there..._"
    "_Working through this one..._"
    "_Bear with me, still on it..._"
)

telegram_send_progress() {
    local chat_id="$1"
    local iteration="$2"
    local index=$(( iteration % ${#PROGRESS_MESSAGES[@]} ))
    local msg="${PROGRESS_MESSAGES[$index]}"
    telegram_send_message "$chat_id" "$msg" > /dev/null 2>&1 || true
}

telegram_parse_webhook() {
    local body="$1"
    # Use printf instead of echo to safely handle untrusted data
    # (echo could misinterpret data starting with -e, -n, etc.)
    # Extract all values in a single jq call for efficiency
    local parsed
    # Use unit separator (0x1F) instead of tab to avoid bash collapsing
    # consecutive whitespace delimiters when fields are empty
    parsed=$(printf '%s' "$body" | jq -r '[
        .message.chat.id // "",
        .message.message_id // "",
        .message.text // "",
        .message.from.id // "",
        .message.reply_to_message.text // "",
        .message.reply_to_message.from.first_name // "",
        (.message.photo[-1].file_id // ""),
        .message.caption // "",
        (.message.document.file_id // ""),
        (.message.document.mime_type // "")
    ] | join("\u001f")')

    # shellcheck disable=SC2034  # WEBHOOK_FROM_ID, WEBHOOK_DOC_* available for use by telegram_get_image_info
    IFS=$'\x1f' read -r WEBHOOK_CHAT_ID WEBHOOK_MESSAGE_ID WEBHOOK_TEXT \
        WEBHOOK_FROM_ID WEBHOOK_REPLY_TO_TEXT WEBHOOK_REPLY_TO_FROM \
        WEBHOOK_PHOTO_FILE_ID WEBHOOK_CAPTION \
        WEBHOOK_DOC_FILE_ID WEBHOOK_DOC_MIME <<< "$parsed"
}

telegram_get_image_info() {
    # Check for compressed photo first (Telegram always sends multiple sizes)
    if [ -n "$WEBHOOK_PHOTO_FILE_ID" ]; then
        WEBHOOK_IMAGE_FILE_ID="$WEBHOOK_PHOTO_FILE_ID"
        WEBHOOK_IMAGE_EXT="jpg"
        return 0
    fi

    # Check for document with image mime type (uncompressed photo)
    if [ -n "$WEBHOOK_DOC_FILE_ID" ] && [[ "$WEBHOOK_DOC_MIME" == image/* ]]; then
        WEBHOOK_IMAGE_FILE_ID="$WEBHOOK_DOC_FILE_ID"
        case "$WEBHOOK_DOC_MIME" in
            image/png)  WEBHOOK_IMAGE_EXT="png" ;;
            image/gif)  WEBHOOK_IMAGE_EXT="gif" ;;
            image/webp) WEBHOOK_IMAGE_EXT="webp" ;;
            *)          WEBHOOK_IMAGE_EXT="jpg" ;;
        esac
        return 0
    fi

    return 1
}

telegram_download_file() {
    local file_id="$1"
    local output_path="$2"

    # Resolve file_id to file_path via Telegram API
    local result
    result=$(telegram_api "getFile" -d "file_id=${file_id}")

    local file_path
    file_path=$(printf '%s' "$result" | jq -r '.result.file_path // empty')

    if [ -z "$file_path" ]; then
        log_error "telegram" "Failed to get file path for file_id: $file_id"
        return 1
    fi

    # Whitelist allowed characters in file_path to prevent path traversal and injection
    if [[ ! "$file_path" =~ ^[a-zA-Z0-9/_.-]+$ ]]; then
        log_error "telegram" "Invalid characters in file path from API"
        return 1
    fi

    # Download the binary file (--max-redirs 0 prevents redirect-based attacks)
    local download_url="https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${file_path}"
    if ! curl -sf --max-redirs 0 -o "$output_path" "$download_url"; then
        log_error "telegram" "Failed to download file: $file_path"
        return 1
    fi

    # Validate file size (max 20 MB — Telegram bot API limit)
    local max_size=$((20 * 1024 * 1024))
    local file_size
    file_size=$(wc -c < "$output_path")
    if [ "$file_size" -gt "$max_size" ]; then
        log_error "telegram" "Downloaded file exceeds size limit: ${file_size} bytes"
        rm -f "$output_path"
        return 1
    fi

    # Validate magic bytes to ensure the file is actually an image
    local header
    header=$(od -An -tx1 -N12 "$output_path" | tr -d ' ')
    case "$header" in
        ffd8ff*)                ;; # JPEG
        89504e47*)              ;; # PNG
        47494638*)              ;; # GIF
        52494646????????57454250)   ;; # WebP (RIFF + 4 size bytes + "WEBP")
        *)
            log_error "telegram" "Downloaded file is not a recognized image format"
            rm -f "$output_path"
            return 1
            ;;
    esac

    log "telegram" "Downloaded file to: $output_path (${file_size} bytes)"
}

telegram_handle_webhook() {
    local body="$1"
    telegram_parse_webhook "$body"

    if [ -z "$WEBHOOK_CHAT_ID" ]; then
        return
    fi

    # Security: only allow configured chat_id
    if [ -n "$TELEGRAM_CHAT_ID" ] && [ "$WEBHOOK_CHAT_ID" != "$TELEGRAM_CHAT_ID" ]; then
        log "telegram" "Rejected message from unauthorized chat_id: $WEBHOOK_CHAT_ID"
        return
    fi

    # Determine if this message contains an image
    local has_image=false
    if telegram_get_image_info; then
        has_image=true
    fi

    # Use text or caption as the message content
    local text="${WEBHOOK_TEXT:-$WEBHOOK_CAPTION}"
    local message_id="$WEBHOOK_MESSAGE_ID"

    # Must have either text or image
    if [ -z "$text" ] && [ "$has_image" != true ]; then
        return
    fi

    # If this is a reply, prepend the original message as context
    if [ -n "$text" ] && [ -n "$WEBHOOK_REPLY_TO_TEXT" ]; then
        local reply_from="${WEBHOOK_REPLY_TO_FROM:-someone}"
        text="[Replying to ${reply_from}: \"${WEBHOOK_REPLY_TO_TEXT}\"]

${text}"
    fi

    # Handle commands (from text or caption)
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
        /voice)
            if [[ "$VOICE_MODE" == "1" ]]; then
                VOICE_MODE="0"
                claudio_save_env
                telegram_send_message "$WEBHOOK_CHAT_ID" "_Voice mode disabled._" "$message_id"
            else
                if [[ -z "$ELEVENLABS_API_KEY" ]]; then
                    telegram_send_message "$WEBHOOK_CHAT_ID" "_Cannot enable voice mode: ELEVENLABS_API_KEY not configured._" "$message_id"
                    return
                fi
                VOICE_MODE="1"
                claudio_save_env
                telegram_send_message "$WEBHOOK_CHAT_ID" "_Voice mode enabled. Responses will be sent as audio._" "$message_id"
            fi
            return
            ;;
        /start)
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Hola!_ Send me a message and I'll forward it to Claude Code." "$message_id"
            return
            ;;
    esac

    log "telegram" "Received message from chat_id=$WEBHOOK_CHAT_ID"

    # Download image if present (after command check to avoid unnecessary downloads)
    local image_file=""
    if [ "$has_image" = true ]; then
        local img_tmpdir="${CLAUDIO_PATH}/tmp"
        if ! mkdir -p "$img_tmpdir"; then
            log_error "telegram" "Failed to create image temp directory: $img_tmpdir"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't process your image. Please try again." "$message_id"
            return
        fi
        image_file=$(mktemp "${img_tmpdir}/claudio-img-XXXXXX.${WEBHOOK_IMAGE_EXT}")
        if ! telegram_download_file "$WEBHOOK_IMAGE_FILE_ID" "$image_file"; then
            rm -f "$image_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't download your image. Please try again." "$message_id"
            return
        fi
        chmod 600 "$image_file"
    fi

    # Build prompt with image reference
    if [ -n "$image_file" ]; then
        if [ -n "$text" ]; then
            text="[The user sent an image at ${image_file}]

${text}"
        else
            text="[The user sent an image at ${image_file}]

Describe this image."
        fi
    fi

    # Store descriptive text in history (temp file path is meaningless after cleanup)
    local history_text="$text"
    if [ -n "$image_file" ]; then
        local caption="${WEBHOOK_CAPTION:-$WEBHOOK_TEXT}"
        if [ -n "$caption" ]; then
            history_text="[Sent an image with caption: ${caption}]"
        else
            history_text="[Sent an image]"
        fi
    fi
    history_add "user" "$history_text"

    # Send typing indicator first, then progress messages if Claude takes long
    # The subshell monitors its parent PID to self-terminate if the parent
    # is killed (e.g., SIGKILL), which would prevent the RETURN trap from firing
    (
        parent_pid=$$
        telegram_send_typing "$WEBHOOK_CHAT_ID"
        sleep 15
        iteration=0
        while kill -0 "$parent_pid" 2>/dev/null; do
            telegram_send_progress "$WEBHOOK_CHAT_ID" "$iteration"
            ((++iteration))
            sleep 30
        done
    ) &
    local typing_pid=$!
    local tts_file=""
    trap 'kill "$typing_pid" 2>/dev/null; wait "$typing_pid" 2>/dev/null; rm -f "$image_file" "$tts_file"' RETURN

    local response
    response=$(claude_run "$text")

    kill "$typing_pid" 2>/dev/null || true
    wait "$typing_pid" 2>/dev/null || true

    if [ -n "$response" ]; then
        history_add "assistant" "$response"

        # Send voice message if voice mode is enabled
        if [[ "$VOICE_MODE" == "1" ]] && [[ -n "$ELEVENLABS_API_KEY" ]]; then
            local tts_tmpdir="${CLAUDIO_PATH}/tmp"
            if ! mkdir -p "$tts_tmpdir"; then
                log_error "telegram" "Failed to create TTS temp directory: $tts_tmpdir"
                telegram_send_message "$WEBHOOK_CHAT_ID" "$response" "$message_id"
            else
                tts_file=$(mktemp "${tts_tmpdir}/claudio-tts-XXXXXX.mp3")
                chmod 600 "$tts_file"

                if tts_convert "$response" "$tts_file"; then
                    if ! telegram_send_voice "$WEBHOOK_CHAT_ID" "$tts_file" "$message_id"; then
                        log_error "telegram" "Failed to send voice message, sending text only"
                    fi
                    # Also send text as follow-up for reference
                    telegram_send_message "$WEBHOOK_CHAT_ID" "$response"
                else
                    # TTS failed, fall back to text only
                    log_error "telegram" "TTS conversion failed, sending text only"
                    telegram_send_message "$WEBHOOK_CHAT_ID" "$response" "$message_id"
                fi
            fi
        else
            telegram_send_message "$WEBHOOK_CHAT_ID" "$response" "$message_id"
        fi
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
    telegram_send_message "$TELEGRAM_CHAT_ID" "👋 Hola! Please return to your terminal to complete the webhook setup."

    # Verify tunnel is configured
    if [ -z "$WEBHOOK_URL" ]; then
        print_warning "No tunnel configured. Run 'claudio install' first."
        exit 1
    fi

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

    print_success "Setup complete!"
}
