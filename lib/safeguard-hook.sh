#!/bin/bash
# PreToolUse hook for Claude Code: prevents Claude from restarting/stopping
# its own service when running inside a Claudio webhook handler.
#
# Registered in ~/.claude/settings.json by `claudio install` and fires
# on every Bash tool call, inspecting the full command string.
#
# Only blocks when CLAUDIO_WEBHOOK_ACTIVE=1 (set by the webhook server).
# Outside that context (e.g., interactive Claude Code usage), all commands
# pass through unmodified.

set -uo pipefail

# Emit a deny JSON response and log the blocked command, then exit.
_deny_and_exit() {
    jq -n '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: "BLOCKED by Claudio safeguard: service management commands that would kill your own process are not allowed from a webhook handler. Changes to lib/*.sh take effect on the next webhook automatically."
        }
    }' 2>/dev/null || printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"BLOCKED by Claudio safeguard: destructive service commands are not allowed from a webhook handler."}}\n'
    local local_log="${CLAUDIO_PATH:-${HOME}/.claudio}/claudio.log"
    printf '[%s] [safeguard-hook] BLOCKED: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(printf '%s' "${COMMAND:-unknown}" | tr -d '\n')" >> "$local_log" 2>/dev/null || true
    exit 0
}

# Only intercept when running inside a webhook handler
if [ "${CLAUDIO_WEBHOOK_ACTIVE:-}" != "1" ]; then
    exit 0
fi

# Parse the command from hook JSON input. Fail-closed: if jq is missing or
# the input is malformed, deny the command rather than allowing it through.
INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null) || _deny_and_exit
[ -n "$COMMAND" ] || exit 0

# Check for systemctl destructive commands targeting claudio
if printf '%s' "$COMMAND" | grep -qE 'systemctl\b' && \
   printf '%s' "$COMMAND" | grep -qE '\b(restart|stop|kill|reload-or-restart|try-restart)\b' && \
   printf '%s' "$COMMAND" | grep -qE '\bclaudio(\.service)?\b'; then
    _deny_and_exit
fi

# Check for launchctl destructive commands targeting claudio
if printf '%s' "$COMMAND" | grep -qE 'launchctl\b' && \
   printf '%s' "$COMMAND" | grep -qE '\b(stop|unload|bootout)\b' && \
   printf '%s' "$COMMAND" | grep -qi 'claudio'; then
    _deny_and_exit
fi

# Also catch: launchctl kickstart -k ... claudio
if printf '%s' "$COMMAND" | grep -qE 'launchctl\b' && \
   printf '%s' "$COMMAND" | grep -q 'kickstart' && \
   printf '%s' "$COMMAND" | grep -q -- '-k' && \
   printf '%s' "$COMMAND" | grep -qi 'claudio'; then
    _deny_and_exit
fi

# Allow everything else
exit 0
