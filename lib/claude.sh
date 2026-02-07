#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

claude_run() {
    local prompt="$1"
    local context
    context=$(history_get_context)

    # Retrieve relevant memories
    local memories=""
    if type memory_retrieve &>/dev/null; then
        memories=$(memory_retrieve "$prompt") || true
    fi

    local full_prompt=""
    if [ -n "$memories" ]; then
        full_prompt="<recalled-memories>"$'\n'"$memories"$'\n'"</recalled-memories>"$'\n\n'
    fi
    if [ -n "$context" ]; then
        full_prompt+="<conversation-history>"$'\n'"$context"$'\n'"</conversation-history>"
        full_prompt+=$'\n\n'"Now respond to this new message:"$'\n\n'"$prompt"
    else
        full_prompt+="$prompt"
    fi

    local -a claude_args=(
        --dangerously-skip-permissions
        --disable-slash-commands
        --model "$MODEL"
        --no-chrome
        --no-session-persistence
        --permission-mode bypassPermissions
        -p -
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

    local response stderr_output out_file prompt_file
    stderr_output=$(mktemp)
    out_file=$(mktemp)
    prompt_file=$(mktemp)
    chmod 600 "$out_file" "$prompt_file"
    printf '%s' "$full_prompt" > "$prompt_file"
    # Ensure temp files are cleaned up even on unexpected exit
    trap 'rm -f "$stderr_output" "$out_file" "$prompt_file"' RETURN

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

    # Run claude in its own session/process group to prevent its child
    # processes (bash tools) from killing the webhook handler via process
    # group signals (e.g., kill 0). Output goes to a temp file so we can
    # recover partial output if claude is killed mid-response.
    # Cross-platform: setsid on Linux, perl POSIX::setsid on macOS.
    if command -v setsid > /dev/null 2>&1; then
        setsid "$claude_cmd" "${claude_args[@]}" < "$prompt_file" > "$out_file" 2>"$stderr_output" &
    else
        perl -e 'use POSIX qw(setsid); setsid(); exec @ARGV' -- \
            "$claude_cmd" "${claude_args[@]}" < "$prompt_file" > "$out_file" 2>"$stderr_output" &
    fi
    local claude_pid=$!

    # Forward SIGTERM to claude's process group if we get killed
    # (e.g., by Python's webhook timeout), then let execution continue
    # so we can still read whatever output claude produced before dying
    trap 'kill -TERM -- -"$claude_pid" 2>/dev/null; wait "$claude_pid" 2>/dev/null || true' TERM

    wait "$claude_pid" || true
    trap - TERM

    response=$(cat "$out_file")

    if [ -s "$stderr_output" ]; then
        log "claude" "$(cat "$stderr_output")"
    fi

    printf '%s\n' "$response"
}
