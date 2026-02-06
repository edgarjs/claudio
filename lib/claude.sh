#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

claude_run() {
    local prompt="$1"
    local context
    context=$(history_get_context)

    local full_prompt=""
    if [ -n "$context" ]; then
        printf -v full_prompt '%s---\n\nNow respond to this new message:\n\n%s' "$context" "$prompt"
    else
        full_prompt="$prompt"
    fi

    local -a claude_args=(
        --dangerously-skip-permissions
        --disable-slash-commands
        --model "$MODEL"
        --no-chrome
        --no-session-persistence
        --permission-mode bypassPermissions
        -p "$full_prompt"
    )

    if [ -f "$CLAUDIO_PROMPT_FILE" ]; then
        local system_prompt
        system_prompt=$(cat "$CLAUDIO_PROMPT_FILE")
        if [ -n "$system_prompt" ]; then
            claude_args+=(--append-system-prompt "$system_prompt")
        fi
    fi

    # Only add fallback model if it differs from the primary model
    if [ "$MODEL" != "haiku" ]; then
        claude_args+=(--fallback-model haiku)
    fi

    local response stderr_output
    stderr_output=$(mktemp)
    # Ensure temp file is cleaned up even on unexpected exit
    trap 'rm -f "$stderr_output"' RETURN

    # Find claude command, trying multiple common locations
    # Note: Don't use 'command -v' as it's a bash builtin that doesn't work correctly
    # when PATH is modified by parent processes (e.g., Python subprocess)
    local claude_cmd
    local home="${HOME:-}"
    if [ -z "$home" ]; then
        home=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6) || \
            home=$(dscl . -read "/Users/$(id -un)" NFSHomeDirectory 2>/dev/null | awk '{print $2}') || \
            home=$(eval echo "~")
    fi
    if [ -z "$home" ]; then
        log "claude" "Error: Cannot determine HOME directory"
        return 1
    fi
    if [ -x "$home/.local/bin/claude" ]; then
        claude_cmd="$home/.local/bin/claude"
    elif [ -x "/opt/homebrew/bin/claude" ]; then
        claude_cmd="/opt/homebrew/bin/claude"
    elif [ -x "/usr/local/bin/claude" ]; then
        claude_cmd="/usr/local/bin/claude"
    elif [ -x "/usr/bin/claude" ]; then
        claude_cmd="/usr/bin/claude"
    else
        log "claude" "Error: claude command not found in common locations"
        return 1
    fi

    response=$("$claude_cmd" "${claude_args[@]}" 2>"$stderr_output") || true

    if [ -s "$stderr_output" ]; then
        log "claude" "$(cat "$stderr_output")"
    fi

    printf '%s\n' "$response"
}
