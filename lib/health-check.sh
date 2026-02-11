#!/bin/bash

# Webhook health check script - calls /health endpoint which verifies and fixes webhook
# Intended to be run periodically via cron (every 5 minutes)
# Auto-restarts the service if it's unreachable (throttled to once per 3 minutes)
# Sends a Telegram alert after 3 restart attempts if the service never recovers
#
# Additional checks (run when service is healthy):
# - Disk usage alerts (configurable threshold, default 90%)
# - Log rotation (configurable max size, default 10MB)
# - Backup freshness (alerts if last backup is older than threshold)
# - Recent log analysis (scans for errors, rapid restarts, API slowness)

set -euo pipefail

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"
# shellcheck source=lib/telegram.sh
source "$(dirname "${BASH_SOURCE[0]}")/telegram.sh"

CLAUDIO_PATH="$HOME/.claudio"
CLAUDIO_ENV_FILE="$CLAUDIO_PATH/service.env"
RESTART_STAMP="$CLAUDIO_PATH/.last_restart_attempt"
FAIL_COUNT_FILE="$CLAUDIO_PATH/.restart_fail_count"
MIN_RESTART_INTERVAL=180  # 3 minutes in seconds
MAX_RESTART_ATTEMPTS=3
DISK_USAGE_THRESHOLD="${DISK_USAGE_THRESHOLD:-90}"       # percentage
LOG_MAX_SIZE="${LOG_MAX_SIZE:-10485760}"                  # 10MB in bytes
BACKUP_MAX_AGE="${BACKUP_MAX_AGE:-7200}"                 # 2 hours in seconds
BACKUP_DEST="${BACKUP_DEST:-/mnt/ssd}"
LOG_CHECK_WINDOW="${LOG_CHECK_WINDOW:-300}"               # 5 minutes lookback
LOG_ALERT_COOLDOWN="${LOG_ALERT_COOLDOWN:-1800}"          # 30 min between log alerts
LOG_ALERT_STAMP="$CLAUDIO_PATH/.last_log_alert"

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

# Ensure XDG_RUNTIME_DIR is set on Linux (cron doesn't provide it, needed for systemctl --user)
if [[ "$(uname)" != "Darwin" ]]; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
fi

# Send a Telegram alert message via telegram_send_message (which handles
# retries, chunking, and parse-mode fallback).
_send_alert() {
    local message="$1"
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
        log_error "health-check" "Cannot send alert: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
        return 1
    fi
    telegram_send_message "$TELEGRAM_CHAT_ID" "$message"
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
    tmp=$(mktemp "${FAIL_COUNT_FILE}.XXXXXX") || return 1
    printf '%s' "$1" > "$tmp"
    mv -f "$tmp" "$FAIL_COUNT_FILE"
}

# Store epoch timestamp in stamp file (portable across GNU/BSD)
_touch_stamp() {
    local tmp
    tmp=$(mktemp "${RESTART_STAMP}.XXXXXX") || return 1
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

# --- Recent log analysis ---
# Scans claudio.log for error patterns within LOG_CHECK_WINDOW seconds.
# Deduplicates: only alerts once per LOG_ALERT_COOLDOWN per issue category.
# Outputs alert text to stdout (empty if nothing found).
_check_recent_logs() {
    local log_file="$CLAUDIO_LOG_FILE"
    [[ -f "$log_file" ]] || return 0

    # Throttle: skip if we alerted recently
    if [[ -f "$LOG_ALERT_STAMP" ]]; then
        local last_alert now
        last_alert=$(cat "$LOG_ALERT_STAMP" 2>/dev/null) || last_alert=0
        now=$(date +%s)
        if [[ "$last_alert" =~ ^[0-9]+$ ]] && (( now - last_alert < LOG_ALERT_COOLDOWN )); then
            return 0
        fi
    fi

    local cutoff_time
    if [[ "$(uname)" == "Darwin" ]]; then
        cutoff_time=$(date -v-"${LOG_CHECK_WINDOW}"S '+%Y-%m-%d %H:%M:%S' 2>/dev/null) || return 0
    else
        cutoff_time=$(date -d "-${LOG_CHECK_WINDOW} seconds" '+%Y-%m-%d %H:%M:%S' 2>/dev/null) || return 0
    fi

    # Extract recent lines (within time window)
    local recent_lines
    recent_lines=$(awk -v cutoff="$cutoff_time" '
        match($0, /^\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\]/, m) {
            if (m[1] >= cutoff) print
        }
    ' "$log_file")

    [[ -z "$recent_lines" ]] && return 0

    local issues=""

    # 1. ERROR lines (excluding health-check's own "Could not connect" which is already handled)
    local filtered_errors
    filtered_errors=$(echo "$recent_lines" | grep 'ERROR:' | grep -v 'Could not connect to server' | grep -v 'Cannot send alert' 2>/dev/null || true)
    local real_error_count
    real_error_count=$(echo "$filtered_errors" | grep -c '.' 2>/dev/null || echo 0)
    if (( real_error_count > 0 )); then
        local sample
        sample=$(echo "$filtered_errors" | tail -1 | sed 's/^\[[^]]*\] //')
        issues="${issues}${real_error_count} error(s): \`${sample}\`"$'\n'
    fi

    # 2. Rapid server restarts (multiple "Starting Claudio server" in window)
    local restart_count
    restart_count=$(echo "$recent_lines" | grep -c 'Starting Claudio server' 2>/dev/null || echo 0)
    if (( restart_count >= 3 )); then
        issues="${issues}Server restarted ${restart_count} times in ${LOG_CHECK_WINDOW}s"$'\n'
    fi

    # 3. Claude tool warnings (BashTool pre-flight)
    local preflight_count
    preflight_count=$(echo "$recent_lines" | grep -c 'Pre-flight check is taking longer' 2>/dev/null || echo 0)
    if (( preflight_count >= 3 )); then
        issues="${issues}Claude API slow (${preflight_count} pre-flight warnings)"$'\n'
    fi

    # 4. WARN lines (not already covered above)
    local warn_lines
    warn_lines=$(echo "$recent_lines" | grep 'WARN:' | grep -v 'Disk usage\|Backup stale\|not mounted' 2>/dev/null || true)
    local warn_count
    warn_count=$(echo "$warn_lines" | grep -c '.' 2>/dev/null || echo 0)
    if (( warn_count > 0 )); then
        local warn_sample
        warn_sample=$(echo "$warn_lines" | tail -1 | sed 's/^\[[^]]*\] //')
        issues="${issues}${warn_count} warning(s): \`${warn_sample}\`"$'\n'
    fi

    if [[ -n "$issues" ]]; then
        # Record alert timestamp
        local tmp
        tmp=$(mktemp "${LOG_ALERT_STAMP}.XXXXXX") || true
        if [[ -n "$tmp" ]]; then
            printf '%s' "$(date +%s)" > "$tmp"
            mv -f "$tmp" "$LOG_ALERT_STAMP"
        fi
        printf '%s' "$issues"
    fi
}

# --- Disk usage check ---
# Checks usage of all mounted partitions relevant to Claudio.
# Returns 0 if all OK, 1 if any partition exceeds threshold.
_check_disk_usage() {
    local alert=false
    local line
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local usage mount
        usage=$(echo "$line" | awk '{print $5}' | tr -d '%')
        mount=$(echo "$line" | awk '{print $6}')
        if [[ "$usage" =~ ^[0-9]+$ ]] && (( usage >= DISK_USAGE_THRESHOLD )); then
            log_warn "health-check" "Disk usage high: ${mount} at ${usage}%"
            alert=true
        fi
    done < <(df -P / "$BACKUP_DEST" 2>/dev/null | tail -n +2)

    [[ "$alert" == true ]] && return 1
    return 0
}

# --- Log rotation ---
# Rotates log files that exceed LOG_MAX_SIZE. Keeps one .1 backup.
_rotate_logs() {
    local rotated=0
    local log_file
    for log_file in "$CLAUDIO_PATH"/*.log; do
        [[ -f "$log_file" ]] || continue
        local size
        size=$(stat -c%s "$log_file" 2>/dev/null || stat -f%z "$log_file" 2>/dev/null || echo 0)
        if (( size > LOG_MAX_SIZE )); then
            mv -f "$log_file" "${log_file}.1"
            log "health-check" "Rotated ${log_file} (${size} bytes)"
            rotated=$((rotated + 1))
        fi
    done
    echo "$rotated"
}

# --- Backup freshness check ---
# Checks if the most recent backup is within BACKUP_MAX_AGE seconds.
# Returns 0 if fresh (or no backup dest configured), 1 if stale, 2 if unmounted.
_check_backup_freshness() {
    # Fail loudly if the backup destination looks like an external drive
    # path but isn't mounted (e.g., SSD disconnected via USB error —
    # the dir stays as an empty mount point).
    # Uses findmnt --target which resolves subdirectories correctly.
    if [[ "$BACKUP_DEST" == /mnt/* || "$BACKUP_DEST" == /media/* ]] && [[ -d "$BACKUP_DEST" ]]; then
        local _not_mounted=false
        if command -v findmnt >/dev/null 2>&1; then
            local _mount_target
            _mount_target=$(findmnt --target "$BACKUP_DEST" -n -o TARGET 2>/dev/null) || _mount_target=""
            [[ "$_mount_target" == "/" || -z "$_mount_target" ]] && _not_mounted=true
        elif command -v mountpoint >/dev/null 2>&1; then
            local _mount_root
            _mount_root=$(echo "$BACKUP_DEST" | cut -d/ -f1-3)
            mountpoint -q "$_mount_root" 2>/dev/null || _not_mounted=true
        fi
        if [[ "$_not_mounted" == true ]]; then
            log_warn "health-check" "Backup destination $BACKUP_DEST is not mounted"
            return 2
        fi
    fi

    local backup_dir="$BACKUP_DEST/claudio-backups/hourly"
    [[ -d "$backup_dir" ]] || return 0  # no backups configured yet

    local latest="$backup_dir/latest"
    if [[ ! -L "$latest" && ! -d "$latest" ]]; then
        # No latest symlink — find newest directory
        latest=$(find "$backup_dir" -maxdepth 1 -mindepth 1 -type d | sort | tail -1)
    fi
    [[ -z "$latest" ]] && return 1  # backup dir exists but empty

    # Resolve symlink (readlink -f is GNU-only, not available on macOS)
    [[ -L "$latest" ]] && latest=$(cd "$(dirname "$latest")" && cd "$(dirname "$(readlink "$latest")")" && pwd)/$(basename "$(readlink "$latest")")

    local dir_name
    dir_name=$(basename "$latest")
    # Parse timestamp from directory name (YYYY-MM-DD_HHMM)
    if [[ "$dir_name" =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})_([0-9]{2})([0-9]{2})$ ]]; then
        local backup_epoch
        local date_str="${BASH_REMATCH[1]}-${BASH_REMATCH[2]}-${BASH_REMATCH[3]} ${BASH_REMATCH[4]}:${BASH_REMATCH[5]}"
        if [[ "$(uname)" == "Darwin" ]]; then
            backup_epoch=$(date -j -f "%Y-%m-%d %H:%M" "$date_str" +%s 2>/dev/null) || return 1
        else
            backup_epoch=$(date -d "$date_str" +%s 2>/dev/null) || return 1
        fi
        local now
        now=$(date +%s)
        local age=$(( now - backup_epoch ))
        if (( age > BACKUP_MAX_AGE )); then
            log_warn "health-check" "Backup stale: last backup ${age}s ago (threshold: ${BACKUP_MAX_AGE}s)"
            return 1
        fi
    else
        return 1  # can't parse, assume stale
    fi
    return 0
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

    # --- Additional system checks (only when service is healthy) ---
    alerts=""

    # Disk usage
    if ! _check_disk_usage; then
        alerts="${alerts}Disk usage above ${DISK_USAGE_THRESHOLD}%. "
    fi

    # Log rotation
    rotated=$(_rotate_logs)

    # Backup freshness (returns 0=fresh, 1=stale, 2=unmounted)
    backup_rc=0
    _check_backup_freshness || backup_rc=$?
    if (( backup_rc == 2 )); then
        alerts="${alerts}Backup drive not mounted ($BACKUP_DEST). "
    elif (( backup_rc == 1 )); then
        alerts="${alerts}Backups are stale. "
    fi

    # Recent log analysis
    log_issues=$(_check_recent_logs)
    if [[ -n "$log_issues" ]]; then
        alerts="${alerts}"$'\n'"Log issues detected:"$'\n'"${log_issues}"
    fi

    # Send combined alert if anything needs attention
    # || true: don't let alert delivery failure abort the health check (set -e)
    # _send_alert already logs on failure internally
    if [[ -n "$alerts" ]]; then
        _send_alert "⚠️ Health check warnings: ${alerts}" || true
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
        # || true: don't abort script; _send_alert logs on failure internally
        _send_alert "⚠️ Claudio server is down after $MAX_RESTART_ATTEMPTS restart attempts. Please check the server manually." || true
    fi
    exit 1
else
    log_error "health-check" "Unexpected response (HTTP $http_code): $body"
    exit 1
fi
