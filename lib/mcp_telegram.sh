#!/bin/bash
#
# Minimal MCP (stdio) server for sending Telegram notifications.
# Reads JSON-RPC from stdin, writes JSON-RPC to stdout.
# Uses telegram.sh's send_message function directly — no Python, no deps.
#

set -euo pipefail

# Source telegram functions
# shellcheck source=lib/telegram.sh
source "$(dirname "${BASH_SOURCE[0]}")/telegram.sh"

# Write a JSON-RPC response to stdout (MCP protocol)
_mcp_respond() {
    local id="$1"
    local result="$2"
    printf '%s\n' "{\"jsonrpc\":\"2.0\",\"id\":$id,\"result\":$result}"
}

_mcp_error() {
    local id="$1"
    local code="$2"
    local message="$3"
    printf '%s\n' "{\"jsonrpc\":\"2.0\",\"id\":$id,\"error\":{\"code\":$code,\"message\":$(jq -Rn --arg m "$message" '$m')}}"
}

# Handle a single JSON-RPC request
_mcp_handle() {
    local line="$1"

    local method id
    IFS=$'\t' read -r method id < <(printf '%s' "$line" | jq -r '[.method // "", .id // "null"] | @tsv')

    case "$method" in
        initialize)
            _mcp_respond "$id" '{
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "telegram-notifier", "version": "1.0.0"}
            }'
            ;;
        notifications/initialized)
            # Notification — no response needed
            ;;
        tools/list)
            _mcp_respond "$id" '{
                "tools": [{
                    "name": "send_telegram_message",
                    "description": "Send an async message to the user via Telegram. Use this to send progress updates, partial results, or notifications while you are still working on a task. The message is delivered immediately and independently of your final response. Use Telegram-compatible formatting: *bold*, _italic_, `code`, ```code blocks```.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "The message text to send to the user"
                            }
                        },
                        "required": ["message"]
                    }
                }]
            }'
            ;;
        tools/call)
            local tool_name message
            IFS=$'\t' read -r tool_name message < <(printf '%s' "$line" | jq -r '[.params.name // "", .params.arguments.message // ""] | @tsv')

            if [ "$tool_name" != "send_telegram_message" ]; then
                _mcp_error "$id" -32601 "Unknown tool: $tool_name"
                return
            fi

            if [ -z "$message" ]; then
                _mcp_error "$id" -32602 "Missing required parameter: message"
                return
            fi

            if [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
                _mcp_error "$id" -32000 "TELEGRAM_CHAT_ID not configured"
                return
            fi

            # Send via telegram.sh (inherits TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from env)
            if telegram_send_message "$TELEGRAM_CHAT_ID" "$message" "" >/dev/null; then
                _mcp_respond "$id" '{"content":[{"type":"text","text":"Message sent successfully"}]}'
            else
                _mcp_respond "$id" '{"content":[{"type":"text","text":"Failed to send message"}],"isError":true}'
            fi
            ;;
        *)
            if [ "$id" != "null" ]; then
                _mcp_error "$id" -32601 "Method not found: $method"
            fi
            # Ignore unknown notifications (id=null)
            ;;
    esac
}

# Main loop: read JSON-RPC lines from stdin
while IFS= read -r line; do
    [ -z "$line" ] && continue
    _mcp_handle "$line"
done
