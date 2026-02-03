#!/bin/bash

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

    local response
    response=$(claude "${claude_args[@]}" 2>>"$CLAUDIO_LOG_FILE") || true

    echo "$response"
}
