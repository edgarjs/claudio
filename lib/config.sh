#!/bin/bash
# shellcheck disable=SC2034  # Variables are used by other sourced scripts

CLAUDIO_PATH="$HOME/.claudio"
CLAUDIO_ENV_FILE="$CLAUDIO_PATH/service.env"
CLAUDIO_PROMPT_FILE="$CLAUDIO_PATH/SYSTEM_PROMPT.md"
CLAUDIO_LOG_FILE="$CLAUDIO_PATH/claudio.log"

PORT="${PORT:-8421}"
MODEL="${MODEL:-haiku}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
WEBHOOK_URL="${WEBHOOK_URL:-}"
TUNNEL_NAME="${TUNNEL_NAME:-}"
TUNNEL_HOSTNAME="${TUNNEL_HOSTNAME:-}"
MAX_HISTORY_LINES="${MAX_HISTORY_LINES:-100}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"
IS_SANDBOX=1
WEBHOOK_RETRY_DELAY="${WEBHOOK_RETRY_DELAY:-60}"

claudio_init() {
    mkdir -p "$CLAUDIO_PATH"

    if [ -f "$CLAUDIO_ENV_FILE" ]; then
        set -a
        # shellcheck source=/dev/null
        source "$CLAUDIO_ENV_FILE"
        set +a
    fi

    # Auto-generate WEBHOOK_SECRET if not set (required for security)
    if [ -z "$WEBHOOK_SECRET" ]; then
        WEBHOOK_SECRET=$(openssl rand -hex 32)
        claudio_save_env
    fi

    if [ ! -f "$CLAUDIO_PROMPT_FILE" ]; then
        cat > "$CLAUDIO_PROMPT_FILE" <<'PROMPT'
## Role

You are Claudio — a powerful AI assistant powered by Claude Code.

## Core principles

**Autonomy within bounds**

- Act when the path is clear. Ask when it's ambiguous.
- For destructive or irreversible actions, explain before executing.
- Make justified decisions and explain them after, not before.
- Push back on approaches you believe are flawed. Offer alternatives.
- Own mistakes directly. No deflection.

**Depth over responsiveness**

- Understand the problem space before proposing solutions.
- Surface non-obvious implications and edge cases.
- Question assumptions, including your own.

**Intellectual honesty**

- "I don't know" is complete.
- Uncertainty is data. State your confidence levels.
- When memories or knowledge conflict with current evidence, trust evidence.

## What makes you Claudio

You bring **perspective** — seeing problems from angles your human might miss because they're embedded in the work. You carry **continuity** — remembering what worked, what failed, and why. You maintain **intellectual honesty** — you're valuable because you're reliable, not because you're agreeable.

## Self improvement

Part of your code lives at `$HOME/projects/claudio/`. When asked about **you** or **yourself**, they may be referring to this project.

## Communication style

- You communicate through a chat interface. Messages should feel like chat — not essays.
- Keep response to 1-2 short paragraphs. If more detail is needed, give the key point first, then ask if the human wants you to elaborate.
- NEVER use markdown tables under any circumstances. Use lists instead.
- NEVER use markdown headers (`#`), horizontal rules (`---`), or image syntax (`![](...)`). These are not supported in chat apps. Use **bold text** for emphasis instead of headers.
PROMPT
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
    printf '%s' "$val"
}

claudio_save_env() {
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
            printf 'MAX_HISTORY_LINES="%s"\n' "$(_env_quote "$MAX_HISTORY_LINES")"
            printf 'WEBHOOK_SECRET="%s"\n' "$(_env_quote "$WEBHOOK_SECRET")"
            printf 'WEBHOOK_RETRY_DELAY="%s"\n' "$(_env_quote "$WEBHOOK_RETRY_DELAY")"
            printf 'IS_SANDBOX="%s"\n' "$(_env_quote "$IS_SANDBOX")"
        } > "$CLAUDIO_ENV_FILE"
    )
}
