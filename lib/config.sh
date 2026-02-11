#!/bin/bash
# shellcheck disable=SC2034  # Variables are used by other sourced scripts

export CLAUDIO_PATH="$HOME/.claudio"
CLAUDIO_ENV_FILE="$CLAUDIO_PATH/service.env"
CLAUDIO_LOG_FILE="$CLAUDIO_PATH/claudio.log"

PORT="${PORT:-8421}"
MODEL="${MODEL:-haiku}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
WEBHOOK_URL="${WEBHOOK_URL:-}"
TUNNEL_NAME="${TUNNEL_NAME:-}"
TUNNEL_HOSTNAME="${TUNNEL_HOSTNAME:-}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"
WEBHOOK_RETRY_DELAY="${WEBHOOK_RETRY_DELAY:-60}"
ELEVENLABS_API_KEY="${ELEVENLABS_API_KEY:-}"
ELEVENLABS_VOICE_ID="${ELEVENLABS_VOICE_ID:-iP95p4xoKVk53GoZ742B}"
ELEVENLABS_MODEL="${ELEVENLABS_MODEL:-eleven_multilingual_v2}"
ELEVENLABS_STT_MODEL="${ELEVENLABS_STT_MODEL:-scribe_v1}"
export MEMORY_ENABLED="${MEMORY_ENABLED:-1}"
MEMORY_EMBEDDING_MODEL="${MEMORY_EMBEDDING_MODEL:-sentence-transformers/all-MiniLM-L6-v2}"
MEMORY_CONSOLIDATION_MODEL="${MEMORY_CONSOLIDATION_MODEL:-haiku}"
MAX_HISTORY_LINES="${MAX_HISTORY_LINES:-100}"

# Safe env file loader: only accepts KEY=value or KEY="value" lines
# where KEY matches [A-Z_][A-Z0-9_]*. Reverses _env_quote escaping
# for double-quoted values. Rejects anything that doesn't match.
_safe_load_env() {
    local env_file="$1"
    [ -f "$env_file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip blank lines and comments
        [[ -z "$line" || "$line" == \#* ]] && continue
        # Match KEY="value" (quoted) or KEY=value (unquoted, no spaces)
        if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=\"(.*)\"$ ]]; then
            local key="${BASH_REMATCH[1]}"
            local val="${BASH_REMATCH[2]}"
            # Reverse _env_quote escaping
            val="${val//\\n/$'\n'}"
            val="${val//\\\`/\`}"
            val="${val//\\\$/\$}"
            val="${val//\\\"/\"}"
            val="${val//\\\\/\\}"
            export "$key=$val"
        elif [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=([^[:space:]]*)$ ]]; then
            export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
        else
            # Reject malformed lines silently (defense in depth)
            continue
        fi
    done < "$env_file"
}

claudio_init() {
    mkdir -p "$CLAUDIO_PATH"
    chmod 700 "$CLAUDIO_PATH"

    _safe_load_env "$CLAUDIO_ENV_FILE"

    # Auto-generate WEBHOOK_SECRET if not set (required for security)
    if [ -z "$WEBHOOK_SECRET" ]; then
        WEBHOOK_SECRET=$(openssl rand -hex 32) || {
            echo "Error: Failed to generate WEBHOOK_SECRET (openssl rand failed)" >&2
            return 1
        }
        claudio_save_env
    fi
}

_env_quote() {
    # Escape for double-quoted env file values
    # Compatible with both bash source and systemd EnvironmentFile
    local val="$1"
    val="${val//\\/\\\\}"
    val="${val//\"/\\\"}"
    val="${val//\$/\\\$}"
    val="${val//\`/\\\`}"
    val="${val//$'\n'/\\n}"
    printf '%s' "$val"
}

claude_hooks_install() {
    local settings_file="$HOME/.claude/settings.json"
    local project_dir="${1:?Usage: claude_hooks_install <project_dir>}"
    local hook_cmd="python3 \"${project_dir}/lib/hooks/post-tool-use.py\""

    mkdir -p "$HOME/.claude"

    # Create settings file if it doesn't exist
    if [ ! -f "$settings_file" ]; then
        echo '{}' > "$settings_file"
    fi

    # Check if hook already registered (exact command match)
    if jq -e --arg cmd "$hook_cmd" '
        .hooks.PostToolUse[]?.hooks[]? | select(.command == $cmd)
    ' "$settings_file" > /dev/null 2>&1; then
        return 0
    fi

    # Add the hook, preserving existing settings
    local hook_entry
    hook_entry=$(jq -n --arg cmd "$hook_cmd" '{
        hooks: [{ type: "command", command: $cmd }]
    }')

    local tmp
    tmp=$(mktemp)
    jq --argjson entry "$hook_entry" '
        .hooks.PostToolUse = ((.hooks.PostToolUse // []) + [$entry])
    ' "$settings_file" > "$tmp" && mv "$tmp" "$settings_file"

    echo "Registered PostToolUse hook in $settings_file"
}

claudio_save_env() {
    # Managed variables (written by this function)
    local -a managed_keys=(
        PORT MODEL TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
        WEBHOOK_URL TUNNEL_NAME TUNNEL_HOSTNAME WEBHOOK_SECRET
        WEBHOOK_RETRY_DELAY ELEVENLABS_API_KEY ELEVENLABS_VOICE_ID
        ELEVENLABS_MODEL ELEVENLABS_STT_MODEL MEMORY_ENABLED
        MEMORY_EMBEDDING_MODEL MEMORY_CONSOLIDATION_MODEL MAX_HISTORY_LINES
    )

    # Collect extra (unmanaged) lines from existing file before overwriting
    local extra_lines=""
    if [ -f "$CLAUDIO_ENV_FILE" ]; then
        local managed_pattern
        managed_pattern=$(printf '%s|' "${managed_keys[@]}")
        managed_pattern="^(${managed_pattern%|})="
        while IFS= read -r line || [ -n "$line" ]; do
            # Keep everything except managed variable assignments
            if [[ ! "$line" =~ $managed_pattern ]]; then
                extra_lines+="$line"$'\n'
            fi
        done < "$CLAUDIO_ENV_FILE"
    fi

    # Use restrictive permissions for file with secrets
    (
        umask 077
        # Double-quoted values for bash source + systemd EnvironmentFile compatibility
        {
            printf 'PORT="%s"\n' "$(_env_quote "$PORT")"
            printf 'MODEL="%s"\n' "$(_env_quote "$MODEL")"
            printf 'TELEGRAM_BOT_TOKEN="%s"\n' "$(_env_quote "$TELEGRAM_BOT_TOKEN")"
            printf 'TELEGRAM_CHAT_ID="%s"\n' "$(_env_quote "$TELEGRAM_CHAT_ID")"
            printf 'WEBHOOK_URL="%s"\n' "$(_env_quote "$WEBHOOK_URL")"
            printf 'TUNNEL_NAME="%s"\n' "$(_env_quote "$TUNNEL_NAME")"
            printf 'TUNNEL_HOSTNAME="%s"\n' "$(_env_quote "$TUNNEL_HOSTNAME")"
            printf 'WEBHOOK_SECRET="%s"\n' "$(_env_quote "$WEBHOOK_SECRET")"
            printf 'WEBHOOK_RETRY_DELAY="%s"\n' "$(_env_quote "$WEBHOOK_RETRY_DELAY")"
            printf 'ELEVENLABS_API_KEY="%s"\n' "$(_env_quote "$ELEVENLABS_API_KEY")"
            printf 'ELEVENLABS_VOICE_ID="%s"\n' "$(_env_quote "$ELEVENLABS_VOICE_ID")"
            printf 'ELEVENLABS_MODEL="%s"\n' "$(_env_quote "$ELEVENLABS_MODEL")"
            printf 'ELEVENLABS_STT_MODEL="%s"\n' "$(_env_quote "$ELEVENLABS_STT_MODEL")"
            printf 'MEMORY_ENABLED="%s"\n' "$(_env_quote "$MEMORY_ENABLED")"
            printf 'MEMORY_EMBEDDING_MODEL="%s"\n' "$(_env_quote "$MEMORY_EMBEDDING_MODEL")"
            printf 'MEMORY_CONSOLIDATION_MODEL="%s"\n' "$(_env_quote "$MEMORY_CONSOLIDATION_MODEL")"
            printf 'MAX_HISTORY_LINES="%s"\n' "$(_env_quote "$MAX_HISTORY_LINES")"
            # Preserve unmanaged variables (e.g. HASS_TOKEN, ALEXA_SKILL_ID)
            if [ -n "$extra_lines" ]; then
                printf '%s' "$extra_lines"
            fi
        } > "$CLAUDIO_ENV_FILE"
    )
}
