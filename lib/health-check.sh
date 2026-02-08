#!/bin/bash

# Webhook health check script - calls /health endpoint which verifies and fixes webhook
# Intended to be run periodically via cron (every minute)
# Auto-restarts the service if it's unreachable (throttled to once per 3 minutes)
# Sends a Telegram alert after 3 restart attempts if the service never recovers

set -euo pipefail

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

CLAUDIO_PATH="$HOME/.claudio"
CLAUDIO_ENV_FILE="$CLAUDIO_PATH/service.env"
RESTART_STAMP="$CLAUDIO_PATH/.last_restart_attempt"
FAIL_COUNT_FILE="$CLAUDIO_PATH/.restart_fail_count"
MIN_RESTART_INTERVAL=180  # 3 minutes in seconds
MAX_RESTART_ATTEMPTS=3

# Safe env file loader: only accepts KEY=value or KEY="value" lines
# where KEY matches [A-Z_][A-Z0-9_]*. Reverses _env_quote escaping
# for double-quoted values. Defined here because health-check.sh is standalone.
_safe_load_env() {
    local env_file="$1"
    [ -f "$env_file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=\"(.*)\"$ ]]; then
            local key="${BASH_REMATCH[1]}"
            local val="${BASH_REMATCH[2]}"
            val="${val//\\n/$'\n'}"
            val="${val//\\\`/\`}"
            val="${val//\\\$/\$}"
            val="${val//\\\"/\"}"
            val="${val//\\\\/\\}"
            export "$key=$val"
        elif [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=([^[:space:]]*)$ ]]; then
            export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
        else
            continue
        fi
    done < "$env_file"
}

# Load environment for PORT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
if [ ! -f "$CLAUDIO_ENV_FILE" ]; then
    log_error "health-check" "Environment file not found: $CLAUDIO_ENV_FILE"
    exit 1
fi

_safe_load_env "$CLAUDIO_ENV_FILE"

PORT="${PORT:-8421}"

# Send a Telegram alert message (standalone, no dependency on telegram.sh)
_send_alert() {
    local message="$1"
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
        log_error "health-check" "Cannot send alert: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
        return 1
    fi
    curl -s --connect-timeout 5 --max-time 10 \
        --config <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN") \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${message}" \
        > /dev/null 2>&1 || true
}

# Read current attempt count (0 if file doesn't exist or invalid)
_get_fail_count() {
    local val
    val=$(cat "$FAIL_COUNT_FILE" 2>/dev/null) || val=0
    if [[ "$val" =~ ^[0-9]+$ ]]; then
        echo "$val"
    else
        echo 0
    fi
}

_set_fail_count() {
    local tmp
    tmp=$(mktemp "${FAIL_COUNT_FILE}.XXXXXX")
    printf '%s' "$1" > "$tmp"
    mv -f "$tmp" "$FAIL_COUNT_FILE"
}

# Store epoch timestamp in stamp file (portable across GNU/BSD)
_touch_stamp() {
    local tmp
    tmp=$(mktemp "${RESTART_STAMP}.XXXXXX")
    printf '%s' "$(date +%s)" > "$tmp"
    mv -f "$tmp" "$RESTART_STAMP"
}

_get_stamp_time() {
    local val
    val=$(cat "$RESTART_STAMP" 2>/dev/null) || val=0
    if [[ "$val" =~ ^[0-9]+$ ]]; then
        echo "$val"
    else
        echo 0
    fi
}

_clear_fail_state() {
    rm -f "$RESTART_STAMP" "$FAIL_COUNT_FILE"
}

# Call health endpoint - it will check and fix webhook if needed
response=$(curl -s --connect-timeout 5 --max-time 10 -w "\n%{http_code}" "http://localhost:${PORT}/health" 2>/dev/null || printf '\n000')
http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" = "200" ]; then
    # Service recovered — clear any restart state
    _clear_fail_state

    # Healthy - nothing to log unless there are pending updates
    pending=$(echo "$body" | jq -r '.checks.telegram_webhook.pending_updates // 0' 2>/dev/null || echo "0")
    if [ "$pending" != "0" ] && [ "$pending" != "null" ]; then
        log "health-check" "Health OK (pending updates: $pending)"
    fi
elif [ "$http_code" = "503" ]; then
    log_error "health-check" "Health check returned unhealthy: $body"
    exit 1
elif [ "$http_code" = "000" ]; then
    log_error "health-check" "Could not connect to server on port $PORT"

    # Check if we've already exhausted restart attempts
    fail_count=$(_get_fail_count)
    if (( fail_count >= MAX_RESTART_ATTEMPTS )); then
        log "health-check" "Restart skipped (already attempted $fail_count times, manual intervention required)"
        exit 1
    fi

    # Throttle restart attempts
    if [ -f "$RESTART_STAMP" ]; then
        last_attempt=$(_get_stamp_time)
        now=$(date +%s)
        if (( now - last_attempt < MIN_RESTART_INTERVAL )); then
            log "health-check" "Restart skipped (last attempt $(( now - last_attempt ))s ago, throttle: ${MIN_RESTART_INTERVAL}s)"
            exit 1
        fi
    fi

    # Check if the service unit/plist exists before attempting restart
    can_restart=false
    if [[ "$(uname)" == "Darwin" ]]; then
        if launchctl list 2>/dev/null | grep -q "com.claudio.server"; then
            can_restart=true
        else
            log_error "health-check" "Service plist not found, cannot auto-restart"
        fi
    else
        if systemctl --user list-unit-files 2>/dev/null | grep -q "claudio"; then
            can_restart=true
        else
            # Distinguish between missing unit and inactive user manager
            if [ -f "${SYSTEMD_UNIT:-$HOME/.config/systemd/user/claudio.service}" ]; then
                log_error "health-check" "User systemd manager not running (linger may be disabled). Run: loginctl enable-linger ${USER:-$(id -un)}"
            else
                log_error "health-check" "Service unit not found, cannot auto-restart"
            fi
        fi
    fi

    if [ "$can_restart" = false ]; then
        exit 1
    fi

    # Attempt restart
    _touch_stamp
    restart_ok=false

    if [[ "$(uname)" == "Darwin" ]]; then
        launchctl stop com.claudio.server 2>/dev/null || true
        if launchctl start com.claudio.server; then
            restart_ok=true
        fi
    else
        if systemctl --user restart claudio; then
            restart_ok=true
        fi
    fi

    # Track attempt count regardless of restart command outcome — the service
    # is only considered recovered when the health endpoint returns HTTP 200
    _set_fail_count "$((fail_count + 1))"
    fail_count=$((fail_count + 1))

    if [ "$restart_ok" = true ]; then
        log "health-check" "Service restarted (attempt $fail_count/$MAX_RESTART_ATTEMPTS)"
    else
        rm -f "$RESTART_STAMP"
        log_error "health-check" "Failed to restart service (attempt $fail_count/$MAX_RESTART_ATTEMPTS)"
    fi

    if (( fail_count >= MAX_RESTART_ATTEMPTS )); then
        log_error "health-check" "Max restart attempts reached, sending alert"
        _send_alert "⚠️ Claudio server is down after $MAX_RESTART_ATTEMPTS restart attempts. Please check the server manually."
    fi
    exit 1
else
    log_error "health-check" "Unexpected response (HTTP $http_code): $body"
    exit 1
fi
