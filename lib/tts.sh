#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

ELEVENLABS_API="https://api.elevenlabs.io/v1"
ELEVENLABS_MODEL="${ELEVENLABS_MODEL:-eleven_multilingual_v2}"
TTS_MAX_CHARS=5000  # Conservative limit (API supports up to 10000)

# Convert text to speech using ElevenLabs API
# Outputs an MP3 file path on success
tts_convert() {
    local text="$1"
    local output_file="$2"

    if [[ -z "$ELEVENLABS_API_KEY" ]]; then
        log_error "tts" "ELEVENLABS_API_KEY not configured"
        return 1
    fi

    if [[ -z "$ELEVENLABS_VOICE_ID" ]]; then
        log_error "tts" "ELEVENLABS_VOICE_ID not configured"
        return 1
    fi

    # Strip markdown formatting for cleaner speech
    text=$(tts_strip_markdown "$text")

    if [[ -z "$text" ]]; then
        log_error "tts" "No text to convert after stripping markdown"
        return 1
    fi

    # Truncate if over limit
    if (( ${#text} > TTS_MAX_CHARS )); then
        text="${text:0:$TTS_MAX_CHARS}"
        log "tts" "Text truncated to $TTS_MAX_CHARS characters"
    fi

    # Validate model ID format (matches stt.sh voice/model validation)
    if [[ ! "$ELEVENLABS_MODEL" =~ ^[a-zA-Z0-9_]+$ ]]; then
        log_error "tts" "Invalid ELEVENLABS_MODEL format"
        return 1
    fi

    local json_payload
    json_payload=$(jq -n --arg text "$text" --arg model "$ELEVENLABS_MODEL" \
        '{text: $text, model_id: $model}')

    # Validate voice ID format
    if [[ ! "$ELEVENLABS_VOICE_ID" =~ ^[a-zA-Z0-9]+$ ]]; then
        log_error "tts" "Invalid ELEVENLABS_VOICE_ID format"
        return 1
    fi

    local http_code
    # Pass API key via curl config to avoid exposing it in process list
    http_code=$(curl -s -o "$output_file" -w "%{http_code}" \
        --connect-timeout 10 --max-time 120 \
        --config <(printf 'header = "xi-api-key: %s"\n' "$ELEVENLABS_API_KEY") \
        -X POST "${ELEVENLABS_API}/text-to-speech/${ELEVENLABS_VOICE_ID}?output_format=mp3_44100_128" \
        -H "Content-Type: application/json" \
        -d "$json_payload")

    if [[ "$http_code" != "200" ]]; then
        # Log error details from response body before deleting
        local error_detail
        error_detail=$(head -c 500 "$output_file" 2>/dev/null | tr -d '\0' || true)
        log_error "tts" "ElevenLabs API returned HTTP $http_code: $error_detail"
        rm -f "$output_file"
        return 1
    fi

    # Validate output is actually an audio file (skip if 'file' not available)
    if command -v file >/dev/null 2>&1; then
        local file_type
        file_type=$(file -b "$output_file" 2>/dev/null)
        if [[ ! "$file_type" =~ Audio|MPEG|ADTS ]]; then
            log_error "tts" "ElevenLabs returned non-audio content: $file_type"
            rm -f "$output_file"
            return 1
        fi
    fi

    log "tts" "Generated voice audio: $(wc -c < "$output_file") bytes"
    return 0
}

# Strip markdown formatting for cleaner TTS output
tts_strip_markdown() {
    local text="$1"

    printf '%s' "$text" | awk '
        /^```/ { in_code = !in_code; next }
        !in_code { print }
    ' | sed -E \
        -e 's/`[^`]*`//g' \
        -e 's/\*\*\*([^*]*)\*\*\*/\1/g' \
        -e 's/\*\*([^*]*)\*\*/\1/g' \
        -e 's/\*([^*]*)\*/\1/g' \
        -e 's/___([^_]*)___/\1/g' \
        -e 's/__([^_]*)__/\1/g' \
        -e 's/\b_([^_]*)_\b/\1/g' \
        -e 's/\[([^]]*)\]\([^)]*\)/\1/g' \
        -e 's/^[[:space:]]*[-*+][[:space:]]/  /g' \
        -e '/^$/N;/^\n$/d'
}
