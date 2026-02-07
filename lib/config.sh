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
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"
IS_SANDBOX="${IS_SANDBOX:-}"
WEBHOOK_RETRY_DELAY="${WEBHOOK_RETRY_DELAY:-60}"
ELEVENLABS_API_KEY="${ELEVENLABS_API_KEY:-}"
ELEVENLABS_VOICE_ID="${ELEVENLABS_VOICE_ID:-iP95p4xoKVk53GoZ742B}"
ELEVENLABS_STT_MODEL="${ELEVENLABS_STT_MODEL:-scribe_v1}"
AGENT_MAX_CONCURRENT="${AGENT_MAX_CONCURRENT:-5}"
AGENT_MAX_GLOBAL_CONCURRENT="${AGENT_MAX_GLOBAL_CONCURRENT:-15}"
AGENT_DEFAULT_TIMEOUT="${AGENT_DEFAULT_TIMEOUT:-300}"
AGENT_DEFAULT_MODEL="${AGENT_DEFAULT_MODEL:-haiku}"
AGENT_POLL_INTERVAL="${AGENT_POLL_INTERVAL:-5}"
AGENT_CLEANUP_AGE="${AGENT_CLEANUP_AGE:-24}"
AGENT_MAX_OUTPUT_BYTES="${AGENT_MAX_OUTPUT_BYTES:-524288}"
AGENT_MAX_CONTEXT_BYTES="${AGENT_MAX_CONTEXT_BYTES:-262144}"
MEMORY_ENABLED="${MEMORY_ENABLED:-1}"
MEMORY_EMBEDDING_MODEL="${MEMORY_EMBEDDING_MODEL:-sentence-transformers/all-MiniLM-L6-v2}"
MEMORY_CONSOLIDATION_MODEL="${MEMORY_CONSOLIDATION_MODEL:-haiku}"

claudio_init() {
    mkdir -p "$CLAUDIO_PATH"

    if [ -f "$CLAUDIO_ENV_FILE" ]; then
        set -a
        # shellcheck source=/dev/null
        source "$CLAUDIO_ENV_FILE"
        set +a
    fi

    # Claude Code requires IS_SANDBOX=1 when running as root
    if [ "$(id -u)" -eq 0 ] && [ "$IS_SANDBOX" != "1" ]; then
        IS_SANDBOX=1
        export IS_SANDBOX
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

## Tool constraints

- **Never execute `systemctl restart`, `systemctl stop`, `launchctl stop`, or any command that restarts/stops the Claudio service.** Doing so kills your own process mid-execution, preventing you from delivering a response. Changes to `lib/*.sh` files take effect on the next webhook invocation automatically — no restart is needed. If a restart is truly required (e.g. after changing `server.py`), ask the user to do it manually.
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
    val="${val//$'\n'/\\n}"
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
            printf 'WEBHOOK_SECRET="%s"\n' "$(_env_quote "$WEBHOOK_SECRET")"
            printf 'WEBHOOK_RETRY_DELAY="%s"\n' "$(_env_quote "$WEBHOOK_RETRY_DELAY")"
            printf 'IS_SANDBOX="%s"\n' "$(_env_quote "$IS_SANDBOX")"
            printf 'ELEVENLABS_API_KEY="%s"\n' "$(_env_quote "$ELEVENLABS_API_KEY")"
            printf 'ELEVENLABS_VOICE_ID="%s"\n' "$(_env_quote "$ELEVENLABS_VOICE_ID")"
            printf 'ELEVENLABS_STT_MODEL="%s"\n' "$(_env_quote "$ELEVENLABS_STT_MODEL")"
            printf 'AGENT_MAX_CONCURRENT="%s"\n' "$(_env_quote "$AGENT_MAX_CONCURRENT")"
            printf 'AGENT_MAX_GLOBAL_CONCURRENT="%s"\n' "$(_env_quote "$AGENT_MAX_GLOBAL_CONCURRENT")"
            printf 'AGENT_DEFAULT_TIMEOUT="%s"\n' "$(_env_quote "$AGENT_DEFAULT_TIMEOUT")"
            printf 'AGENT_DEFAULT_MODEL="%s"\n' "$(_env_quote "$AGENT_DEFAULT_MODEL")"
            printf 'AGENT_POLL_INTERVAL="%s"\n' "$(_env_quote "$AGENT_POLL_INTERVAL")"
            printf 'AGENT_CLEANUP_AGE="%s"\n' "$(_env_quote "$AGENT_CLEANUP_AGE")"
            printf 'AGENT_MAX_OUTPUT_BYTES="%s"\n' "$(_env_quote "$AGENT_MAX_OUTPUT_BYTES")"
            printf 'AGENT_MAX_CONTEXT_BYTES="%s"\n' "$(_env_quote "$AGENT_MAX_CONTEXT_BYTES")"
            printf 'MEMORY_ENABLED="%s"\n' "$(_env_quote "$MEMORY_ENABLED")"
            printf 'MEMORY_EMBEDDING_MODEL="%s"\n' "$(_env_quote "$MEMORY_EMBEDDING_MODEL")"
            printf 'MEMORY_CONSOLIDATION_MODEL="%s"\n' "$(_env_quote "$MEMORY_CONSOLIDATION_MODEL")"
        } > "$CLAUDIO_ENV_FILE"
    )
}
