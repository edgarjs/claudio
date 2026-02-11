#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

TELEGRAM_API="https://api.telegram.org/bot"

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

telegram_send_voice() {
    local chat_id="$1"
    local audio_file="$2"
    local reply_to_message_id="${3:-}"

    local args=(-F "chat_id=${chat_id}" -F "voice=@${audio_file}")
    if [ -n "$reply_to_message_id" ]; then
        args+=(-F "reply_to_message_id=${reply_to_message_id}")
    fi

    local result
    result=$(telegram_api "sendVoice" "${args[@]}")
    local ok
    ok=$(echo "$result" | jq -r '.ok // empty')
    if [ "$ok" != "true" ]; then
        log_error "telegram" "sendVoice failed: $result"
        return 1
    fi
}

telegram_send_typing() {
    local chat_id="$1"
    local action="${2:-typing}"
    # Fire-and-forget: don't retry typing indicators to avoid rate limit cascades
    curl -s --connect-timeout 5 --max-time 10 \
        --config <(printf 'url = "%s%s/sendChatAction"\n' "$TELEGRAM_API" "$TELEGRAM_BOT_TOKEN") \
        -d "chat_id=${chat_id}" \
        -d "action=${action}" > /dev/null 2>&1 || true
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
        (.message.document.mime_type // ""),
        (.message.document.file_name // ""),
        (.message.voice.file_id // ""),
        (.message.voice.duration // ""),
        ((.message._extra_photos // []) | join(","))
    ] | join("\u001f")')

    # shellcheck disable=SC2034  # WEBHOOK_FROM_ID, WEBHOOK_DOC_*, WEBHOOK_VOICE_* available for use
    # -d '' uses NUL as record delimiter so newlines within fields are preserved
    IFS=$'\x1f' read -r -d '' WEBHOOK_CHAT_ID WEBHOOK_MESSAGE_ID WEBHOOK_TEXT \
        WEBHOOK_FROM_ID WEBHOOK_REPLY_TO_TEXT WEBHOOK_REPLY_TO_FROM \
        WEBHOOK_PHOTO_FILE_ID WEBHOOK_CAPTION \
        WEBHOOK_DOC_FILE_ID WEBHOOK_DOC_MIME WEBHOOK_DOC_FILE_NAME \
        WEBHOOK_VOICE_FILE_ID WEBHOOK_VOICE_DURATION \
        WEBHOOK_EXTRA_PHOTOS <<< "$parsed" || true
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

# Internal helper: resolves file_id, downloads, validates size/empty
_telegram_download_raw() {
    local file_id="$1"
    local output_path="$2"
    local label="$3"

    local result
    result=$(telegram_api "getFile" -d "file_id=${file_id}")

    local file_path
    file_path=$(printf '%s' "$result" | jq -r '.result.file_path // empty')

    if [ -z "$file_path" ]; then
        log_error "telegram" "Failed to get file path for ${label} file_id: $file_id"
        return 1
    fi

    # Whitelist allowed characters in file_path to prevent path traversal and injection
    if [[ ! "$file_path" =~ ^[a-zA-Z0-9/_.-]+$ ]] || [[ "$file_path" == *".."* ]]; then
        log_error "telegram" "Invalid characters in ${label} file path from API"
        return 1
    fi

    # Download the file (--max-redirs 0 prevents redirect-based attacks)
    # Use --config to avoid exposing bot token in process list (ps aux)
    if ! curl -sf --connect-timeout 10 --max-time 60 --max-redirs 0 -o "$output_path" \
        --config <(printf 'url = "https://api.telegram.org/file/bot%s/%s"\n' "$TELEGRAM_BOT_TOKEN" "$file_path"); then
        log_error "telegram" "Failed to download ${label}: $file_path"
        return 1
    fi

    # Validate file size (max 20 MB — Telegram bot API limit)
    local max_size=$((20 * 1024 * 1024))
    local file_size
    file_size=$(wc -c < "$output_path")
    if [ "$file_size" -gt "$max_size" ]; then
        log_error "telegram" "Downloaded ${label} exceeds size limit: ${file_size} bytes"
        rm -f "$output_path"
        return 1
    fi

    if [ "$file_size" -eq 0 ]; then
        log_error "telegram" "Downloaded ${label} is empty"
        rm -f "$output_path"
        return 1
    fi

    log "telegram" "Downloaded ${label} to: $output_path (${file_size} bytes)"
}

telegram_download_file() {
    local file_id="$1"
    local output_path="$2"

    if ! _telegram_download_raw "$file_id" "$output_path" "image"; then
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
}

telegram_download_document() {
    _telegram_download_raw "$1" "$2" "document"
}

telegram_download_voice() {
    local file_id="$1"
    local output_path="$2"

    if ! _telegram_download_raw "$file_id" "$output_path" "voice"; then
        return 1
    fi

    # Validate magic bytes to ensure the file is actually an OGG audio file
    local header
    header=$(od -An -tx1 -N4 "$output_path" | tr -d ' ')
    case "$header" in
        4f676753) ;; # OGG (Telegram voice = OGG Opus)
        *)
            log_error "telegram" "Downloaded voice file is not a recognized audio format"
            rm -f "$output_path"
            return 1
            ;;
    esac
}

telegram_handle_webhook() {
    local body="$1"
    telegram_parse_webhook "$body"

    if [ -z "$WEBHOOK_CHAT_ID" ]; then
        return
    fi

    # Security: only allow configured chat_id (never skip if unset)
    if [ -z "$TELEGRAM_CHAT_ID" ]; then
        log_error "telegram" "TELEGRAM_CHAT_ID not configured — rejecting all messages"
        return
    fi
    if [ "$WEBHOOK_CHAT_ID" != "$TELEGRAM_CHAT_ID" ]; then
        log "telegram" "Rejected message from unauthorized chat_id: $WEBHOOK_CHAT_ID"
        return
    fi

    # Determine if this message contains an image
    local has_image=false
    if telegram_get_image_info; then
        has_image=true
    fi

    # Determine if this message contains a document (non-image file)
    local has_document=false
    if [ -n "$WEBHOOK_DOC_FILE_ID" ] && [[ "$WEBHOOK_DOC_MIME" != image/* ]]; then
        has_document=true
    fi

    # Determine if this message contains a voice message
    local has_voice=false
    if [ -n "$WEBHOOK_VOICE_FILE_ID" ]; then
        has_voice=true
    fi

    # Use text or caption as the message content
    local text="${WEBHOOK_TEXT:-$WEBHOOK_CAPTION}"
    local message_id="$WEBHOOK_MESSAGE_ID"

    # Must have either text, image, document, or voice
    if [ -z "$text" ] && [ "$has_image" != true ] && [ "$has_document" != true ] && [ "$has_voice" != true ]; then
        return
    fi

    # Handle commands BEFORE prepending reply context, so commands work
    # even when sent as replies to other messages
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

    # If this is a reply, prepend the original message as context
    # Sanitize reply text to prevent prompt injection via crafted messages
    if [ -n "$text" ] && [ -n "$WEBHOOK_REPLY_TO_TEXT" ]; then
        local reply_from
        reply_from=$(printf '%s' "${WEBHOOK_REPLY_TO_FROM:-someone}" | _sanitize_for_prompt)
        local sanitized_reply
        sanitized_reply=$(printf '%s' "$WEBHOOK_REPLY_TO_TEXT" | _sanitize_for_prompt)
        text="[Replying to ${reply_from}: \"${sanitized_reply}\"]

${text}"
    fi

    log "telegram" "Received message from chat_id=$WEBHOOK_CHAT_ID"

    # Download image(s) if present (after command check to avoid unnecessary downloads)
    local image_file=""
    local -a extra_image_files=()
    if [ "$has_image" = true ]; then
        local img_tmpdir="${CLAUDIO_PATH}/tmp"
        if ! mkdir -p "$img_tmpdir"; then
            log_error "telegram" "Failed to create image temp directory: $img_tmpdir"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't process your image. Please try again." "$message_id"
            return
        fi
        image_file=$(mktemp "${img_tmpdir}/claudio-img-XXXXXX.${WEBHOOK_IMAGE_EXT}") || {
            log_error "telegram" "Failed to create temp file for image"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't process your image. Please try again." "$message_id"
            return
        }
        if ! telegram_download_file "$WEBHOOK_IMAGE_FILE_ID" "$image_file"; then
            rm -f "$image_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't download your image. Please try again." "$message_id"
            return
        fi
        chmod 600 "$image_file"

        # Download extra photos from media group (if any).
        # _extra_photos is injected by _merge_media_group() in server.py.
        if [ -n "$WEBHOOK_EXTRA_PHOTOS" ]; then
            IFS=',' read -ra _extra_ids <<< "$WEBHOOK_EXTRA_PHOTOS"
            for _fid in "${_extra_ids[@]}"; do
                [ -z "$_fid" ] && continue
                local _efile
                _efile=$(mktemp "${img_tmpdir}/claudio-img-XXXXXX.jpg") || continue
                if telegram_download_file "$_fid" "$_efile"; then
                    chmod 600 "$_efile"
                    extra_image_files+=("$_efile")
                else
                    rm -f "$_efile"
                    log_error "telegram" "Failed to download extra photo from media group"
                fi
            done
            log "telegram" "Downloaded $((${#extra_image_files[@]} + 1)) photos from media group"
        fi
    fi

    # Download document if present (after command check to avoid unnecessary downloads)
    local doc_file=""
    if [ "$has_document" = true ]; then
        local doc_tmpdir="${CLAUDIO_PATH}/tmp"
        if ! mkdir -p "$doc_tmpdir"; then
            log_error "telegram" "Failed to create document temp directory: $doc_tmpdir"
            rm -f "$image_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't process your file. Please try again." "$message_id"
            return
        fi
        # Derive extension from original file name, fallback to mime type
        local doc_ext="bin"
        if [ -n "$WEBHOOK_DOC_FILE_NAME" ]; then
            local name_ext="${WEBHOOK_DOC_FILE_NAME##*.}"
            if [ -n "$name_ext" ] && [ "$name_ext" != "$WEBHOOK_DOC_FILE_NAME" ] && [[ "$name_ext" =~ ^[a-zA-Z0-9]+$ ]] && [ ${#name_ext} -le 10 ]; then
                doc_ext="$name_ext"
            fi
        fi
        doc_file=$(mktemp "${doc_tmpdir}/claudio-doc-XXXXXX.${doc_ext}") || {
            log_error "telegram" "Failed to create temp file for document"
            rm -f "$image_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't process your file. Please try again." "$message_id"
            return
        }
        if ! telegram_download_document "$WEBHOOK_DOC_FILE_ID" "$doc_file"; then
            rm -f "$doc_file" "$image_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't download your file. Please try again." "$message_id"
            return
        fi
        chmod 600 "$doc_file"
    fi

    # Download and transcribe voice message if present
    local voice_file=""
    local transcription=""
    if [ "$has_voice" = true ]; then
        if [[ -z "$ELEVENLABS_API_KEY" ]]; then
            rm -f "$image_file" "$doc_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "_Voice messages require ELEVENLABS_API_KEY to be configured._" "$message_id"
            return
        fi
        local voice_tmpdir="${CLAUDIO_PATH}/tmp"
        if ! mkdir -p "$voice_tmpdir"; then
            log_error "telegram" "Failed to create voice temp directory: $voice_tmpdir"
            rm -f "$image_file" "$doc_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't process your voice message. Please try again." "$message_id"
            return
        fi
        voice_file=$(mktemp "${voice_tmpdir}/claudio-voice-XXXXXX.oga") || {
            log_error "telegram" "Failed to create temp file for voice"
            rm -f "$image_file" "$doc_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't process your voice message. Please try again." "$message_id"
            return
        }
        if ! telegram_download_voice "$WEBHOOK_VOICE_FILE_ID" "$voice_file"; then
            rm -f "$voice_file" "$image_file" "$doc_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't download your voice message. Please try again." "$message_id"
            return
        fi
        chmod 600 "$voice_file"

        if ! transcription=$(stt_transcribe "$voice_file"); then
            rm -f "$voice_file" "$image_file" "$doc_file"
            telegram_send_message "$WEBHOOK_CHAT_ID" "Sorry, I couldn't transcribe your voice message. Please try again." "$message_id"
            return
        fi
        rm -f "$voice_file"
        voice_file=""

        # stt_transcribe guarantees non-empty text on success
        if [ -n "$text" ]; then
            text="${transcription}

${text}"
        else
            text="$transcription"
        fi
        log "telegram" "Voice message transcribed: ${#transcription} chars"
    fi

    # Build prompt with image reference(s)
    if [ -n "$image_file" ]; then
        local image_count=$(( 1 + ${#extra_image_files[@]} ))
        if [ "$image_count" -eq 1 ]; then
            if [ -n "$text" ]; then
                text="[The user sent an image at ${image_file}]

${text}"
            else
                text="[The user sent an image at ${image_file}]

Describe this image."
            fi
        else
            local image_refs="[The user sent ${image_count} images at: ${image_file}"
            for _ef in "${extra_image_files[@]}"; do
                image_refs+=", ${_ef}"
            done
            image_refs+="]"
            if [ -n "$text" ]; then
                text="${image_refs}

${text}"
            else
                text="${image_refs}

Describe these images."
            fi
        fi
    fi

    # Build prompt with document reference
    if [ -n "$doc_file" ]; then
        local doc_name="${WEBHOOK_DOC_FILE_NAME:-document}"
        # Sanitize filename: strip chars that could break prompt framing or enable injection
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

    # Store descriptive text in history (temp file path is meaningless after cleanup)
    local history_text="$text"
    if [ "$has_voice" = true ]; then
        history_text="[Sent a voice message: ${transcription}]"
    elif [ -n "$image_file" ]; then
        local caption="${WEBHOOK_CAPTION:-$WEBHOOK_TEXT}"
        if [ ${#extra_image_files[@]} -gt 0 ]; then
            local img_total=$(( 1 + ${#extra_image_files[@]} ))
            if [ -n "$caption" ]; then
                history_text="[Sent ${img_total} images with caption: ${caption}]"
            else
                history_text="[Sent ${img_total} images]"
            fi
        elif [ -n "$caption" ]; then
            history_text="[Sent an image with caption: ${caption}]"
        else
            history_text="[Sent an image]"
        fi
    elif [ -n "$doc_file" ]; then
        local caption="${WEBHOOK_CAPTION:-$WEBHOOK_TEXT}"
        if [ -n "$caption" ]; then
            history_text="[Sent a file \"${doc_name}\" with caption: ${caption}]"
        else
            history_text="[Sent a file \"${doc_name}\"]"
        fi
    fi
    # Send typing indicator while Claude is working
    # Telegram typing status lasts ~5s; we resend every 4s for continuous feedback
    # The subshell monitors its parent PID to self-terminate if the parent
    # is killed (e.g., SIGKILL), which would prevent the RETURN trap from firing
    local typing_action="typing"
    [ "$has_voice" = true ] && typing_action="record_voice"
    (
        parent_pid=$$
        while kill -0 "$parent_pid" 2>/dev/null; do
            telegram_send_typing "$WEBHOOK_CHAT_ID" "$typing_action"
            sleep 4
        done
    ) &
    local typing_pid=$!
    local tts_file=""
    trap 'kill "$typing_pid" 2>/dev/null; wait "$typing_pid" 2>/dev/null; rm -f "$image_file" "$doc_file" "$voice_file" "$tts_file" "${extra_image_files[@]}"' RETURN

    local response
    response=$(claude_run "$text")

    # Enrich no-caption document history with summary from Claude's response.
    # Images are intentionally excluded: including image descriptions in history
    # biases future invocations into "recognizing" the same content instead of
    # actually looking at the new images passed to them.
    if [ -n "$response" ]; then
        if [ -z "${WEBHOOK_CAPTION:-$WEBHOOK_TEXT}" ]; then
            if [ -n "$doc_file" ]; then
                history_text="[Sent a file \"${doc_name}\": $(_summarize "$response")]"
            fi
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

        # Consolidate memories post-response (background — doesn't block further processing)
        if type memory_consolidate &>/dev/null; then
            (memory_consolidate || true) &
        fi

        # Respond with voice when the user sent a voice message
        # (ELEVENLABS_API_KEY is guaranteed non-empty here — checked at voice download)
        if [ "$has_voice" = true ]; then
            local tts_tmpdir="${CLAUDIO_PATH}/tmp"
            if ! mkdir -p "$tts_tmpdir"; then
                log_error "telegram" "Failed to create TTS temp directory: $tts_tmpdir"
                telegram_send_message "$WEBHOOK_CHAT_ID" "$response" "$message_id"
            else
                tts_file=$(mktemp "${tts_tmpdir}/claudio-tts-XXXXXX.mp3") || {
                    log_error "telegram" "Failed to create temp file for TTS"
                    telegram_send_message "$WEBHOOK_CHAT_ID" "$response" "$message_id"
                    return
                }
                chmod 600 "$tts_file"

                if tts_convert "$response" "$tts_file"; then
                    if ! telegram_send_voice "$WEBHOOK_CHAT_ID" "$tts_file" "$message_id"; then
                        log_error "telegram" "Failed to send voice message, falling back to text"
                        telegram_send_message "$WEBHOOK_CHAT_ID" "$response" "$message_id"
                    fi
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
