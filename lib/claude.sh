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

    # Generate MCP config with absolute paths (tempfile cleaned up on return)
    local mcp_config notifier_log
    mcp_config=$(mktemp)
    notifier_log=$(mktemp)
    chmod 600 "$mcp_config" "$notifier_log"
    local lib_dir
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    jq -n \
        --arg path "${lib_dir}/mcp_telegram.py" \
        --arg token "${TELEGRAM_BOT_TOKEN}" \
        --arg chat_id "${TELEGRAM_CHAT_ID}" \
        --arg log_file "${notifier_log}" \
        '{
            mcpServers: {
                "telegram-notifier": {
                    command: "python3",
                    args: [ $path ],
                    env: {
                        TELEGRAM_BOT_TOKEN: $token,
                        TELEGRAM_CHAT_ID: $chat_id,
                        NOTIFIER_LOG_FILE: $log_file
                    }
                }
            }
        }' > "$mcp_config"

    local -a claude_args=(
        --disable-slash-commands
        --mcp-config "$mcp_config"
        --model "$MODEL"
        --no-chrome
        --no-session-persistence
        --output-format json
        --tools "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,Task,TaskOutput,TaskStop,TodoWrite,mcp__telegram-notifier__send_telegram_message"
        --allowedTools "Read" "Write" "Edit" "Bash" "Glob" "Grep" "WebFetch" "WebSearch" "Task" "TaskOutput" "TaskStop" "TodoWrite" "mcp__telegram-notifier__send_telegram_message"
        -p -
    )

    local prompt_source
    prompt_source="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/SYSTEM_PROMPT.md"
    if [ -f "$prompt_source" ]; then
        local system_prompt
        system_prompt=$(cat "$prompt_source")
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
    trap 'rm -f "$stderr_output" "$out_file" "$prompt_file" "$mcp_config" "$notifier_log"' RETURN

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

    # Prevent Claude from spawning background tasks that would outlive
    # this one-shot webhook invocation
    export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1

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

    local raw_output
    raw_output=$(cat "$out_file")

    if [ -s "$stderr_output" ]; then
        log "claude" "$(cat "$stderr_output")"
    fi

    # Parse JSON output: extract response text and persist usage stats
    if [ -n "$raw_output" ]; then
        response=$(printf '%s' "$raw_output" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('result', ''), end='')
except (json.JSONDecodeError, KeyError):
    # Fallback: treat as plain text (e.g., if --output-format json wasn't honored)
    sys.exit(1)
" 2>/dev/null) || response="$raw_output"

        # Persist token usage in background
        _claude_persist_usage "$raw_output" &
    fi

    # Read notifier messages so caller can include them in conversation history.
    # Must happen before RETURN trap deletes the temp file.
    # shellcheck disable=SC2034  # Used by telegram.sh
    CLAUDE_NOTIFIER_MESSAGES=""
    if [ -s "$notifier_log" ]; then
        # shellcheck disable=SC2034
        CLAUDE_NOTIFIER_MESSAGES=$(jq -r '"[Notification: \(.)]"' "$notifier_log" 2>/dev/null) || true
    fi

    printf '%s\n' "$response"
}

_claude_persist_usage() {
    local raw_json="$1"
    # Pass JSON via stdin to avoid exceeding MAX_ARG_STRLEN (128KB) on large responses
    printf '%s' "$raw_json" | python3 -c "
import sys, json, os

try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

usage = data.get('usage', {})
model_usage = data.get('modelUsage', {})
model = next(iter(model_usage), None) if model_usage else None

db_path = os.environ.get('CLAUDIO_DB_FILE', '')
if not db_path:
    sys.exit(0)

import sqlite3
conn = sqlite3.connect(db_path, timeout=10)
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('PRAGMA busy_timeout=5000')
try:
    conn.execute(
        '''INSERT INTO token_usage
           (model, input_tokens, output_tokens, cache_read_tokens,
            cache_creation_tokens, cost_usd, duration_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (
            model,
            usage.get('input_tokens', 0),
            usage.get('output_tokens', 0),
            usage.get('cache_read_input_tokens', 0),
            usage.get('cache_creation_input_tokens', 0),
            data.get('total_cost_usd', 0),
            data.get('duration_ms', 0),
        )
    )
    conn.commit()
finally:
    conn.close()
" 2>/dev/null || true
}
