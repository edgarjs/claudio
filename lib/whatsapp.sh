#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

WHATSAPP_API="https://graph.facebook.com/v21.0"

# Helper: Create secure temporary config file for curl
# Returns path via stdout, caller must cleanup
_whatsapp_curl_config() {
    local endpoint="$1"
    local config_file
    config_file=$(mktemp "${CLAUDIO_PATH}/tmp/curl-config-XXXXXX") || return 1
    chmod 600 "$config_file"

    printf 'url = "%s/%s/%s"\n' "$WHATSAPP_API" "$WHATSAPP_PHONE_NUMBER_ID" "$endpoint" > "$config_file"
    printf 'header = "Authorization: Bearer %s"\n' "$WHATSAPP_ACCESS_TOKEN" >> "$config_file"

    echo "$config_file"
}

# Strip XML-like tags that could be used for prompt injection
_sanitize_for_prompt() {
    sed -E 's/<\/?[a-zA-Z_][a-zA-Z0-9_-]*[^>]*>/[quoted text]/g'
}

# Collapse text to a single line, trimmed and truncated to 200 chars
_summarize() {
    local summary
    summary=$(printf '%s' "$1" | _sanitize_for_prompt | tr '\n' ' ' | sed -E 's/^[[:space:]]*//;s/[[:space:]]+/ /g')
    [ ${#summary} -gt 200 ] && summary="${summary:0:200}..."
    printf '%s' "$summary"
}

whatsapp_api() {
    local endpoint="$1"
    shift

    local max_retries=4
    local attempt=0
    local response http_code body
    local config_file

    # Create secure config file (prevents credential exposure in process list)
    config_file=$(_whatsapp_curl_config "$endpoint") || {
        log_error "whatsapp" "Failed to create curl config"
        return 1
    }
    trap 'rm -f "$config_file"' RETURN

    while [ $attempt -le $max_retries ]; do
        response=$(curl -s -w "\n%{http_code}" --config "$config_file" "$@")
        http_code=$(echo "$response" | tail -n1)
        body=$(echo "$response" | sed '$d')

        # Success or client error (4xx except 429) - don't retry
        if [[ "$http_code" =~ ^2 ]] || { [[ "$http_code" =~ ^4 ]] && [ "$http_code" != "429" ]; }; then
            echo "$body"
            return 0
        fi

        # Retryable: 429 (rate limit) or 5xx (server error)
        if [ $attempt -lt $max_retries ]; then
            local delay=$(( 2 ** attempt ))  # Exponential backoff
            log "whatsapp" "API error (HTTP $http_code), retrying in ${delay}s..."
            sleep "$delay"
        fi

        ((attempt++)) || true
    done

    # All retries exhausted
    log_error "whatsapp" "API failed after $((max_retries + 1)) attempts (HTTP $http_code)"
    echo "$body"
    return 1
}

whatsapp_send_message() {
    local to="$1"
    local text="$2"
    local reply_to_message_id="${3:-}"

    # WhatsApp has a 4096 char limit per message
    local max_len=4096
    local is_first=true
    while [ ${#text} -gt 0 ]; do
        local chunk="${text:0:$max_len}"
        text="${text:$max_len}"

        # Build JSON payload with jq for safe variable handling
        local payload
        if [ "$is_first" = true ] && [ -n "$reply_to_message_id" ]; then
            payload=$(jq -n \
                --arg to "$to" \
                --arg text "$chunk" \
                --arg mid "$reply_to_message_id" \
                '{
                    messaging_product: "whatsapp",
                    recipient_type: "individual",
                    to: $to,
                    type: "text",
                    text: { preview_url: false, body: $text }
                } | . + {context: {message_id: $mid}}')
        else
            payload=$(jq -n \
                --arg to "$to" \
                --arg text "$chunk" \
                '{
                    messaging_product: "whatsapp",
                    recipient_type: "individual",
                    to: $to,
                    type: "text",
                    text: { preview_url: false, body: $text }
                }')
        fi
        is_first=false

        local result
        result=$(whatsapp_api "messages" \
            -H "Content-Type: application/json" \
            -d "$payload")

        local success
        success=$(echo "$result" | jq -r '.messages[0].id // empty' 2>/dev/null)
        if [ -z "$success" ]; then
            log_error "whatsapp" "Failed to send message: $result"
        fi
    done
}

whatsapp_send_audio() {
    local to="$1"
    local audio_file="$2"
    local reply_to_message_id="${3:-}"

    # Upload audio file and send
    local mime_type="audio/mpeg"  # MP3
    local config_file result

    # Create secure config for media upload
    config_file=$(mktemp "${CLAUDIO_PATH}/tmp/curl-config-XXXXXX") || {
        log_error "whatsapp" "Failed to create curl config"
        return 1
    }
    chmod 600 "$config_file"
    trap 'rm -f "$config_file"' RETURN

    printf 'url = "%s/%s/media"\n' "$WHATSAPP_API" "$WHATSAPP_PHONE_NUMBER_ID" > "$config_file"
    printf 'header = "Authorization: Bearer %s"\n' "$WHATSAPP_ACCESS_TOKEN" >> "$config_file"

    result=$(curl -s --config "$config_file" \
        -H "Content-Type: multipart/form-data" \
        -F "messaging_product=whatsapp" \
        -F "file=@${audio_file};type=${mime_type}")

    local media_id
    media_id=$(echo "$result" | jq -r '.id // empty')
    if [ -z "$media_id" ]; then
        log_error "whatsapp" "Failed to upload audio: $result"
        return 1
    fi

    # Send audio message with media_id - use jq for safe JSON construction
    local payload
    payload=$(jq -n \
        --arg to "$to" \
        --arg mid "$media_id" \
        --arg rmid "$reply_to_message_id" \
        '{
            messaging_product: "whatsapp",
            recipient_type: "individual",
            to: $to,
            type: "audio",
            audio: { id: $mid }
        } | if $rmid != "" then . + {context: {message_id: $rmid}} else . end')

    result=$(whatsapp_api "messages" \
        -H "Content-Type: application/json" \
        -d "$payload")

    local success
    success=$(echo "$result" | jq -r '.messages[0].id // empty')
    if [ -z "$success" ]; then
        log_error "whatsapp" "Failed to send audio message: $result"
        return 1
    fi
}

# whatsapp_send_typing removed - WhatsApp Cloud API typing indicator requires
# message_id and auto-dismisses after 25s. Proper implementation deferred to follow-up.
# See: https://github.com/edgarjs/claudio/issues/XXX

whatsapp_mark_read() {
    local message_id="$1"
    # Fire-and-forget: don't retry read receipts
    curl -s --connect-timeout 5 --max-time 10 \
        --config <(printf 'url = "%s/%s/messages"\n' "$WHATSAPP_API" "$WHATSAPP_PHONE_NUMBER_ID"; printf 'header = "Authorization: Bearer %s"\n' "$WHATSAPP_ACCESS_TOKEN") \
        -H "Content-Type: application/json" \
        -d "{\"messaging_product\":\"whatsapp\",\"status\":\"read\",\"message_id\":\"${message_id}\"}" \
        > /dev/null 2>&1 || true
}

whatsapp_parse_webhook() {
    local body="$1"
    # Extract message data from WhatsApp webhook format
    # WhatsApp sends: entry[0].changes[0].value.messages[0]
    local parsed
    parsed=$(printf '%s' "$body" | jq -r '[
        .entry[0].changes[0].value.messages[0].from // "",
        .entry[0].changes[0].value.messages[0].id // "",
        .entry[0].changes[0].value.messages[0].text.body // "",
        .entry[0].changes[0].value.messages[0].type // "",
        (.entry[0].changes[0].value.messages[0].image.id // ""),
        (.entry[0].changes[0].value.messages[0].image.caption // ""),
        (.entry[0].changes[0].value.messages[0].document.id // ""),
        (.entry[0].changes[0].value.messages[0].document.filename // ""),
        (.entry[0].changes[0].value.messages[0].document.mime_type // ""),
        (.entry[0].changes[0].value.messages[0].audio.id // ""),
        (.entry[0].changes[0].value.messages[0].voice.id // ""),
        (.entry[0].changes[0].value.messages[0].context.id // "")
    ] | join("\u001f")')

    # shellcheck disable=SC2034  # Variables available for use
    IFS=$'\x1f' read -r -d '' WEBHOOK_FROM_NUMBER WEBHOOK_MESSAGE_ID WEBHOOK_TEXT \
        WEBHOOK_MESSAGE_TYPE WEBHOOK_IMAGE_ID WEBHOOK_IMAGE_CAPTION \
        WEBHOOK_DOC_ID WEBHOOK_DOC_FILENAME WEBHOOK_DOC_MIME \
        WEBHOOK_AUDIO_ID WEBHOOK_VOICE_ID WEBHOOK_CONTEXT_ID <<< "$parsed" || true
}

whatsapp_download_media() {
    local media_id="$1"
    local output_path="$2"
    local label="${3:-media}"
    local config_file

    # Step 1: Get media URL from WhatsApp API
    config_file=$(mktemp "${CLAUDIO_PATH}/tmp/curl-config-XXXXXX") || {
        log_error "whatsapp" "Failed to create curl config"
        return 1
    }
    chmod 600 "$config_file"
    trap 'rm -f "$config_file"' RETURN

    printf 'url = "%s/%s"\n' "$WHATSAPP_API" "$media_id" > "$config_file"
    printf 'header = "Authorization: Bearer %s"\n' "$WHATSAPP_ACCESS_TOKEN" >> "$config_file"

    local url_response
    url_response=$(curl -s --connect-timeout 10 --max-time 30 --config "$config_file")

    local media_url
    media_url=$(printf '%s' "$url_response" | jq -r '.url // empty')

    if [ -z "$media_url" ]; then
        log_error "whatsapp" "Failed to get ${label} URL for media_id: $media_id"
        return 1
    fi

    # Whitelist allowed characters to prevent injection
    if [[ ! "$media_url" =~ ^https:// ]]; then
        log_error "whatsapp" "Invalid ${label} URL scheme"
        return 1
    fi

    # Step 2: Download the media file
    # Reuse config file for download - use _env_quote to prevent injection
    printf 'url = "%s"\n' "$(_env_quote "$media_url")" > "$config_file"
    printf 'header = "Authorization: Bearer %s"\n' "$WHATSAPP_ACCESS_TOKEN" >> "$config_file"

    if ! curl -sf --connect-timeout 10 --max-time 60 --max-redirs 1 -o "$output_path" --config "$config_file"; then
        log_error "whatsapp" "Failed to download ${label}"
        return 1
    fi

    # Validate file size (max 16 MB — WhatsApp Cloud API limit)
    local max_size=$((16 * 1024 * 1024))
    local file_size
    file_size=$(wc -c < "$output_path")
    if [ "$file_size" -gt "$max_size" ]; then
        log_error "whatsapp" "Downloaded ${label} exceeds size limit: ${file_size} bytes"
        rm -f "$output_path"
        return 1
    fi

    if [ "$file_size" -eq 0 ]; then
        log_error "whatsapp" "Downloaded ${label} is empty"
        rm -f "$output_path"
        return 1
    fi

    log "whatsapp" "Downloaded ${label} to: $output_path (${file_size} bytes)"
}

whatsapp_download_image() {
    local media_id="$1"
    local output_path="$2"

    if ! whatsapp_download_media "$media_id" "$output_path" "image"; then
        return 1
    fi

    # Validate magic bytes to ensure it's an image
    local header
    header=$(od -An -tx1 -N12 "$output_path" | tr -d ' ')
    case "$header" in
        ffd8ff*)                ;; # JPEG
        89504e47*)              ;; # PNG
        47494638*)              ;; # GIF
        52494646????????57454250)   ;; # WebP
        *)
            log_error "whatsapp" "Downloaded file is not a recognized image format"
            rm -f "$output_path"
            return 1
            ;;
    esac
}

whatsapp_download_document() {
    whatsapp_download_media "$1" "$2" "document"
}

whatsapp_download_audio() {
    local media_id="$1"
    local output_path="$2"

    if ! whatsapp_download_media "$media_id" "$output_path" "audio"; then
        return 1
    fi

    # Validate magic bytes for audio formats
    local header
    header=$(od -An -tx1 -N12 "$output_path" | tr -d ' ')
    case "$header" in
        4f676753*) ;; # OGG
        494433*)   ;; # MP3 (ID3 tag)
        fffb*)     ;; # MP3 (frame sync)
        fff3*)     ;; # MP3 (MPEG-1 Layer 3)
        fff2*)     ;; # MP3 (MPEG-2 Layer 3)
        *)
            log_error "whatsapp" "Downloaded file is not a recognized audio format"
            rm -f "$output_path"
            return 1
            ;;
    esac
}

whatsapp_handle_webhook() {
    local body="$1"
    whatsapp_parse_webhook "$body"

    if [ -z "$WEBHOOK_FROM_NUMBER" ]; then
        return
    fi

    # Security: only allow configured phone number (never skip if unset)
    if [ -z "$WHATSAPP_PHONE_NUMBER" ]; then
        log_error "whatsapp" "WHATSAPP_PHONE_NUMBER not configured — rejecting all messages"
        return
    fi
    if [ "$WEBHOOK_FROM_NUMBER" != "$WHATSAPP_PHONE_NUMBER" ]; then
        log "whatsapp" "Rejected message from unauthorized number: $WEBHOOK_FROM_NUMBER"
        return
    fi

    local text="$WEBHOOK_TEXT"
    local message_id="$WEBHOOK_MESSAGE_ID"

    # Handle different message types
    local has_image=false
    local has_document=false
    local has_audio=false

    case "$WEBHOOK_MESSAGE_TYPE" in
        image)
            has_image=true
            text="${WEBHOOK_IMAGE_CAPTION:-$text}"
            ;;
        document)
            has_document=true
            ;;
        audio|voice)
            has_audio=true
            ;;
        text)
            # Already handled
            ;;
        *)
            log "whatsapp" "Unsupported message type: $WEBHOOK_MESSAGE_TYPE"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I don't support that message type yet." "$message_id"
            return
            ;;
    esac

    # Must have either text, image, document, or audio
    if [ -z "$text" ] && [ "$has_image" != true ] && [ "$has_document" != true ] && [ "$has_audio" != true ]; then
        return
    fi

    # If this is a reply, prepend context note
    # (WhatsApp doesn't provide the original message text, only the message ID)
    if [ -n "$text" ] && [ -n "$WEBHOOK_CONTEXT_ID" ]; then
        text="[Replying to a previous message]

${text}"
    fi

    # Handle commands
    case "$text" in
        /opus)
            MODEL="opus"
            if [ -n "$CLAUDIO_BOT_DIR" ]; then
                claudio_save_bot_env
            else
                claudio_save_env
            fi
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "_Switched to Opus model._" "$message_id"
            return
            ;;
        /sonnet)
            MODEL="sonnet"
            if [ -n "$CLAUDIO_BOT_DIR" ]; then
                claudio_save_bot_env
            else
                claudio_save_env
            fi
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "_Switched to Sonnet model._" "$message_id"
            return
            ;;
        /haiku)
            # shellcheck disable=SC2034  # Used by claude.sh via config
            MODEL="haiku"
            if [ -n "$CLAUDIO_BOT_DIR" ]; then
                claudio_save_bot_env
            else
                claudio_save_env
            fi
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "_Switched to Haiku model._" "$message_id"
            return
            ;;
        /start)
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "_Hola!_ Send me a message and I'll forward it to Claude Code." "$message_id"
            return
            ;;
    esac

    log "whatsapp" "Received message from number=$WEBHOOK_FROM_NUMBER"

    # Mark as read to acknowledge receipt
    whatsapp_mark_read "$message_id"

    # Download image if present
    local image_file=""
    if [ "$has_image" = true ] && [ -n "$WEBHOOK_IMAGE_ID" ]; then
        local img_tmpdir="${CLAUDIO_PATH}/tmp"
        if ! mkdir -p "$img_tmpdir"; then
            log_error "whatsapp" "Failed to create image temp directory: $img_tmpdir"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't process your image. Please try again." "$message_id"
            return
        fi
        image_file=$(mktemp "${img_tmpdir}/claudio-img-XXXXXX.jpg") || {
            log_error "whatsapp" "Failed to create temp file for image"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't process your image. Please try again." "$message_id"
            return
        }
        if ! whatsapp_download_image "$WEBHOOK_IMAGE_ID" "$image_file"; then
            rm -f "$image_file"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't download your image. Please try again." "$message_id"
            return
        fi
        chmod 600 "$image_file"
    fi

    # Download document if present
    local doc_file=""
    if [ "$has_document" = true ] && [ -n "$WEBHOOK_DOC_ID" ]; then
        local doc_tmpdir="${CLAUDIO_PATH}/tmp"
        if ! mkdir -p "$doc_tmpdir"; then
            log_error "whatsapp" "Failed to create document temp directory: $doc_tmpdir"
            rm -f "$image_file"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't process your file. Please try again." "$message_id"
            return
        fi
        # Derive extension from filename
        local doc_ext="bin"
        if [ -n "$WEBHOOK_DOC_FILENAME" ]; then
            local name_ext="${WEBHOOK_DOC_FILENAME##*.}"
            if [ -n "$name_ext" ] && [ "$name_ext" != "$WEBHOOK_DOC_FILENAME" ] && [[ "$name_ext" =~ ^[a-zA-Z0-9]+$ ]] && [ ${#name_ext} -le 10 ]; then
                doc_ext="$name_ext"
            fi
        fi
        doc_file=$(mktemp "${doc_tmpdir}/claudio-doc-XXXXXX.${doc_ext}") || {
            log_error "whatsapp" "Failed to create temp file for document"
            rm -f "$image_file"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't process your file. Please try again." "$message_id"
            return
        }
        if ! whatsapp_download_document "$WEBHOOK_DOC_ID" "$doc_file"; then
            rm -f "$doc_file" "$image_file"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't download your file. Please try again." "$message_id"
            return
        fi
        chmod 600 "$doc_file"
    fi

    # Download and transcribe audio if present
    local audio_file=""
    local transcription=""
    if [ "$has_audio" = true ] && [ -n "${WEBHOOK_AUDIO_ID}${WEBHOOK_VOICE_ID}" ]; then
        if [[ -z "$ELEVENLABS_API_KEY" ]]; then
            rm -f "$image_file" "$doc_file"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "_Voice messages require ELEVENLABS_API_KEY to be configured._" "$message_id"
            return
        fi
        local audio_tmpdir="${CLAUDIO_PATH}/tmp"
        if ! mkdir -p "$audio_tmpdir"; then
            log_error "whatsapp" "Failed to create audio temp directory: $audio_tmpdir"
            rm -f "$image_file" "$doc_file"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't process your audio message. Please try again." "$message_id"
            return
        fi
        audio_file=$(mktemp "${audio_tmpdir}/claudio-audio-XXXXXX.ogg") || {
            log_error "whatsapp" "Failed to create temp file for audio"
            rm -f "$image_file" "$doc_file"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't process your audio message. Please try again." "$message_id"
            return
        }
        local audio_id="${WEBHOOK_AUDIO_ID:-$WEBHOOK_VOICE_ID}"
        if ! whatsapp_download_audio "$audio_id" "$audio_file"; then
            rm -f "$audio_file" "$image_file" "$doc_file"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't download your audio message. Please try again." "$message_id"
            return
        fi
        chmod 600 "$audio_file"

        if ! transcription=$(stt_transcribe "$audio_file"); then
            rm -f "$audio_file" "$image_file" "$doc_file"
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't transcribe your audio message. Please try again." "$message_id"
            return
        fi
        rm -f "$audio_file"
        audio_file=""

        if [ -n "$text" ]; then
            text="${transcription}

${text}"
        else
            text="$transcription"
        fi
        log "whatsapp" "Audio message transcribed: ${#transcription} chars"
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

    # Build prompt with document reference
    if [ -n "$doc_file" ]; then
        local doc_name="${WEBHOOK_DOC_FILENAME:-document}"
        doc_name=$(printf '%s' "$doc_name" | tr -cd 'a-zA-Z0-9._ -' | head -c 255)
        doc_name="${doc_name:-document}"
        if [ -n "$text" ]; then
            text="[The user sent a file \"${doc_name}\" at ${doc_file}]

${text}"
        else
            text="[The user sent a file \"${doc_name}\" at ${doc_file}]

Read this file and summarize its contents."
        fi
    fi

    # Store descriptive text in history
    local history_text="$text"
    if [ "$has_audio" = true ]; then
        history_text="[Sent an audio message: ${transcription}]"
    elif [ -n "$image_file" ]; then
        local caption="${WEBHOOK_IMAGE_CAPTION:-}"
        if [ -n "$caption" ]; then
            history_text="[Sent an image with caption: ${caption}]"
        else
            history_text="[Sent an image]"
        fi
    elif [ -n "$doc_file" ]; then
        if [ -n "$text" ]; then
            history_text="[Sent a file \"${doc_name}\" with caption: ${text}]"
        else
            history_text="[Sent a file \"${doc_name}\"]"
        fi
    fi

    # Typing indicator removed - see whatsapp_send_typing comment above
    local tts_file=""
    trap 'rm -f "$image_file" "$doc_file" "$audio_file" "$tts_file"' RETURN

    local response
    response=$(claude_run "$text")

    # Enrich history with document summary
    if [ -n "$response" ]; then
        if [ -z "$WEBHOOK_IMAGE_CAPTION" ] && [ -n "$doc_file" ]; then
            history_text="[Sent a file \"${doc_name}\": $(_summarize "$response")]"
        fi
    fi

    history_add "user" "$history_text"

    if [ -n "$response" ]; then
        local history_response="$response"
        if [ -n "${CLAUDE_NOTIFIER_MESSAGES:-}" ]; then
            history_response="${CLAUDE_NOTIFIER_MESSAGES}"$'\n\n'"${history_response}"
        fi
        if [ -n "${CLAUDE_TOOL_SUMMARY:-}" ]; then
            history_response="${CLAUDE_TOOL_SUMMARY}"$'\n\n'"${history_response}"
        fi
        history_response=$(printf '%s' "$history_response" | _sanitize_for_prompt)
        history_add "assistant" "$history_response"

        # Consolidate memories
        if type memory_consolidate &>/dev/null; then
            (memory_consolidate || true) &
        fi

        # Respond with audio when the user sent an audio message
        # (ELEVENLABS_API_KEY is guaranteed non-empty here — checked at audio download)
        if [ "$has_audio" = true ]; then
            local tts_tmpdir="${CLAUDIO_PATH}/tmp"
            if ! mkdir -p "$tts_tmpdir"; then
                log_error "whatsapp" "Failed to create TTS temp directory: $tts_tmpdir"
                whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "$response" "$message_id"
            else
                tts_file=$(mktemp "${tts_tmpdir}/claudio-tts-XXXXXX.mp3") || {
                    log_error "whatsapp" "Failed to create temp file for TTS"
                    whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "$response" "$message_id"
                    return
                }
                chmod 600 "$tts_file"

                if tts_convert "$response" "$tts_file"; then
                    if ! whatsapp_send_audio "$WEBHOOK_FROM_NUMBER" "$tts_file" "$message_id"; then
                        log_error "whatsapp" "Failed to send audio message, falling back to text"
                        whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "$response" "$message_id"
                    fi
                else
                    # TTS failed, fall back to text only
                    log_error "whatsapp" "TTS conversion failed, sending text only"
                    whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "$response" "$message_id"
                fi
            fi
        else
            whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "$response" "$message_id"
        fi
    else
        whatsapp_send_message "$WEBHOOK_FROM_NUMBER" "Sorry, I couldn't get a response. Please try again." "$message_id"
    fi
}

whatsapp_setup() {
    local bot_id="${1:-}"

    echo "=== Claudio WhatsApp Business API Setup ==="
    if [ -n "$bot_id" ]; then
        echo "Bot: $bot_id"
    fi
    echo ""
    echo "You'll need the following from your WhatsApp Business account:"
    echo "1. Phone Number ID (from Meta Business Suite)"
    echo "2. Access Token (permanent token from Meta for Developers)"
    echo "3. App Secret (from your Meta app settings)"
    echo "4. Authorized phone number (the number you want to receive messages from)"
    echo ""

    read -rp "Enter your WhatsApp Phone Number ID: " phone_id
    if [ -z "$phone_id" ]; then
        print_error "Phone Number ID cannot be empty."
        exit 1
    fi

    read -rp "Enter your WhatsApp Access Token: " access_token
    if [ -z "$access_token" ]; then
        print_error "Access Token cannot be empty."
        exit 1
    fi

    read -rp "Enter your WhatsApp App Secret: " app_secret
    if [ -z "$app_secret" ]; then
        print_error "App Secret cannot be empty."
        exit 1
    fi

    read -rp "Enter authorized phone number (format: 1234567890): " phone_number
    if [ -z "$phone_number" ]; then
        print_error "Phone number cannot be empty."
        exit 1
    fi

    # Generate verify token
    local verify_token
    verify_token=$(openssl rand -hex 32) || {
        print_error "Failed to generate verify token"
        exit 1
    }

    export WHATSAPP_PHONE_NUMBER_ID="$phone_id"
    export WHATSAPP_ACCESS_TOKEN="$access_token"
    export WHATSAPP_APP_SECRET="$app_secret"
    export WHATSAPP_PHONE_NUMBER="$phone_number"
    export WHATSAPP_VERIFY_TOKEN="$verify_token"

    # Verify credentials by calling the API
    local config_file test_result
    config_file=$(mktemp "${CLAUDIO_PATH}/tmp/curl-config-XXXXXX") || {
        print_error "Failed to create temporary config file"
        exit 1
    }
    chmod 600 "$config_file"
    trap 'rm -f "$config_file"' RETURN

    printf 'url = "%s/%s"\n' "$WHATSAPP_API" "$phone_id" > "$config_file"
    printf 'header = "Authorization: Bearer %s"\n' "$access_token" >> "$config_file"

    test_result=$(curl -s --connect-timeout 10 --max-time 30 --config "$config_file")

    local verified_name
    verified_name=$(echo "$test_result" | jq -r '.verified_name // empty')
    if [ -z "$verified_name" ]; then
        print_error "Failed to verify WhatsApp credentials. Check your Phone Number ID and Access Token."
        exit 1
    fi

    print_success "Credentials verified: $verified_name"

    # Verify tunnel is configured
    if [ -z "$WEBHOOK_URL" ]; then
        print_warning "No tunnel configured. Run 'claudio install' first."
        exit 1
    fi

    # Save config: per-bot or global
    if [ -n "$bot_id" ]; then
        # Validate bot_id format
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
        if [ -f "$bot_dir/bot.env" ]; then
            # shellcheck source=/dev/null
            source "$bot_dir/bot.env" 2>/dev/null || true
        fi

        # Re-apply new WhatsApp credentials (source may have overwritten them during re-configuration)
        export WHATSAPP_PHONE_NUMBER_ID="$phone_id"
        export WHATSAPP_ACCESS_TOKEN="$access_token"
        export WHATSAPP_APP_SECRET="$app_secret"
        export WHATSAPP_PHONE_NUMBER="$phone_number"
        export WHATSAPP_VERIFY_TOKEN="$verify_token"

        claudio_save_bot_env

        print_success "Bot config saved to $bot_dir/bot.env"
    else
        claudio_save_env
        print_success "Config saved to service.env"
    fi

    echo ""
    echo "=== Webhook Configuration ==="
    echo "Configure your WhatsApp webhook in Meta for Developers:"
    echo ""
    echo "  Callback URL: ${WEBHOOK_URL}/whatsapp/webhook"
    echo "  Verify Token: ${verify_token}"
    echo ""
    echo "Subscribe to these webhook fields:"
    echo "  - messages"
    echo ""
    print_success "Setup complete!"
}
