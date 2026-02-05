#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

ELEVENLABS_STT_API="https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_STT_MODEL="${ELEVENLABS_STT_MODEL:-scribe_v1}"

# Transcribe audio file using ElevenLabs Speech-to-Text API
# Usage: stt_transcribe <audio_file>
# Prints transcribed text to stdout
stt_transcribe() {
    local audio_file="$1"

    if [[ -z "$ELEVENLABS_API_KEY" ]]; then
        log_error "stt" "ELEVENLABS_API_KEY not configured"
        return 1
    fi

    if [[ ! -f "$audio_file" ]]; then
        log_error "stt" "Audio file not found: $audio_file"
        return 1
    fi

    local file_size
    file_size=$(wc -c < "$audio_file")
    if [[ "$file_size" -eq 0 ]]; then
        log_error "stt" "Audio file is empty: $audio_file"
        return 1
    fi

    # ElevenLabs STT limit is 3GB, but Telegram voice max is 20MB
    local max_size=$((20 * 1024 * 1024))
    if [[ "$file_size" -gt "$max_size" ]]; then
        log_error "stt" "Audio file too large: ${file_size} bytes (max ${max_size})"
        return 1
    fi

    local response_file
    response_file=$(mktemp)
    trap 'rm -f "$response_file"' RETURN

    # Validate model ID format to prevent injection via curl -F
    if [[ ! "$ELEVENLABS_STT_MODEL" =~ ^[a-zA-Z0-9_]+$ ]]; then
        log_error "stt" "Invalid ELEVENLABS_STT_MODEL format"
        return 1
    fi

    local http_code
    http_code=$(curl -s -o "$response_file" -w "%{http_code}" \
        --connect-timeout 10 --max-time 120 \
        --config <(printf 'header = "xi-api-key: %s"\n' "$ELEVENLABS_API_KEY") \
        -X POST "$ELEVENLABS_STT_API" \
        -F "file=@${audio_file}" \
        -F "model_id=${ELEVENLABS_STT_MODEL}")

    if [[ "$http_code" != "200" ]]; then
        local error_detail
        error_detail=$(head -c 500 "$response_file" 2>/dev/null | tr -d '\0' || true)
        log_error "stt" "ElevenLabs STT API returned HTTP $http_code: $error_detail"
        return 1
    fi

    local text
    text=$(jq -r '.text // empty' "$response_file")

    if [[ -z "$text" ]]; then
        log_error "stt" "ElevenLabs STT returned empty transcription"
        return 1
    fi

    local language
    language=$(jq -r '.language_code // "unknown"' "$response_file")
    log "stt" "Transcribed ${file_size} bytes of audio (language: ${language}, ${#text} chars)"

    printf '%s' "$text"
}
