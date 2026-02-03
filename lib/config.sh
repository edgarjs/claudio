#!/bin/bash

CLAUDIO_PATH="$HOME/.claudio"
CLAUDIO_ENV_FILE="$CLAUDIO_PATH/service.env"
CLAUDIO_PROMPT_FILE="$CLAUDIO_PATH/SYSTEM_PROMPT.md"
CLAUDIO_HISTORY_FILE="$CLAUDIO_PATH/history.jsonl"
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

claudio_init() {
    mkdir -p "$CLAUDIO_PATH"

    if [ -f "$CLAUDIO_ENV_FILE" ]; then
        set -a
        source "$CLAUDIO_ENV_FILE"
        set +a
    fi

    if [ ! -f "$CLAUDIO_PROMPT_FILE" ]; then
        cat > "$CLAUDIO_PROMPT_FILE" <<'PROMPT'
## Communication Style

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
EOF
}
