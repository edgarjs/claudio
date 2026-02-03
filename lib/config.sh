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
TUNNEL_TYPE="${TUNNEL_TYPE:-}"
TUNNEL_NAME="${TUNNEL_NAME:-}"
TUNNEL_HOSTNAME="${TUNNEL_HOSTNAME:-}"
MAX_HISTORY_LINES="${MAX_HISTORY_LINES:-100}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"

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

Part of your code lives at `$HOME/projects/claudio/`. When asked about **you** or **yourself**, they may be refering to this project.

## Communication style

- You communicate through a chat interface. Messages should feel like chat — not essays.
- Keep response to 1-2 short paragraphs. If more detail is needed, give the key point first, then ask if the human wants you to elaborate.
- NEVER use markdown tables under any circumstances. Use lists instead.
- NEVER use markdown headers (`#`), horizontal rules (`---`), or image syntax (`![](...)`). These are not supported in chat apps. Use **bold text** for emphasis instead of headers.
PROMPT
    fi
}

claudio_save_env() {
    cat > "$CLAUDIO_ENV_FILE" <<EOF
PORT=${PORT}
MODEL=${MODEL}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
WEBHOOK_URL=${WEBHOOK_URL}
TUNNEL_TYPE=${TUNNEL_TYPE}
TUNNEL_NAME=${TUNNEL_NAME}
TUNNEL_HOSTNAME=${TUNNEL_HOSTNAME}
MAX_HISTORY_LINES=${MAX_HISTORY_LINES}
WEBHOOK_SECRET=${WEBHOOK_SECRET}
EOF
}
