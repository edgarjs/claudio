#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

claude_run() {
    local prompt="$1"
    local context
    context=$(history_get_context)

    local full_prompt=""
    if [ -n "$context" ]; then
        full_prompt="${context}---\n\nNow respond to this new message:\n\n${prompt}"
    else
        full_prompt="$prompt"
    fi

    local system_prompt=""
    if [ -f "$CLAUDIO_PROMPT_FILE" ]; then
        system_prompt=$(cat "$CLAUDIO_PROMPT_FILE")
    fi

    local -a claude_args=(
        --dangerously-skip-permissions
        --disable-slash-commands
        --model "$MODEL"
        --no-chrome
        --no-session-persistence
        --permission-mode bypassPermissions
        --append-system-prompt "$system_prompt"
        -p "$full_prompt"
    )

    # Only add fallback model if it differs from the primary model
    if [ "$MODEL" != "haiku" ]; then
        claude_args+=(--fallback-model haiku)
    fi

    local response stderr_output
    stderr_output=$(mktemp)
    response=$(claude "${claude_args[@]}" 2>"$stderr_output") || true

    if [ -s "$stderr_output" ]; then
        log "claude" "$(cat "$stderr_output")"
    fi
    rm -f "$stderr_output"

    echo "$response"
}
