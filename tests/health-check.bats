#!/usr/bin/env bats

setup() {
    export BATS_TEST_TMPDIR="${BATS_TEST_TMPDIR:-/tmp/bats-$$}"
    mkdir -p "$BATS_TEST_TMPDIR"
    export HOME="$BATS_TEST_TMPDIR"
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR/.claudio"
    mkdir -p "$CLAUDIO_PATH"

    # Clear any inherited environment variables
    unset TELEGRAM_BOT_TOKEN
    unset WEBHOOK_URL
    unset WEBHOOK_SECRET
    unset PORT

    # Create mock bin directory first in PATH
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    mkdir -p "$BATS_TEST_TMPDIR/bin"

    # Mock systemctl — real systemctl hangs in test environment (no user session)
    cat > "$BATS_TEST_TMPDIR/bin/systemctl" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"--property=MainPID"* ]]; then
    echo "0"
elif [[ "$*" == *"list-unit-files"* ]]; then
    echo "claudio.service enabled"
elif [[ "$*" == *"restart"* ]]; then
    exit 0
elif [[ "$*" == *"is-active"* ]]; then
    exit 1
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/systemctl"

    # Mock pgrep — avoid matching real processes in test environment
    cat > "$BATS_TEST_TMPDIR/bin/pgrep" << 'MOCK'
#!/bin/bash
exit 1
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/pgrep"

    # Default: no backup dir to check (tests override BACKUP_DEST as needed)
    export BACKUP_DEST="$BATS_TEST_TMPDIR/no-backups"
}

teardown() {
    rm -rf "$BATS_TEST_TMPDIR"
}

create_env_file() {
    cat > "$CLAUDIO_PATH/service.env" << EOF
PORT="8421"
TELEGRAM_BOT_TOKEN="test-token-123"
WEBHOOK_URL="https://test.example.com"
WEBHOOK_SECRET="secret123"
EOF
}

create_mock_curl_healthy() {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
echo '{"status":"healthy","checks":{"telegram_webhook":{"status":"ok","pending_updates":0}}}'
echo "200"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"
}

create_mock_curl_unhealthy() {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
echo '{"status":"unhealthy","checks":{"telegram_webhook":{"status":"mismatch"}}}'
echo "503"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"
}

create_mock_curl_server_down() {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
# Simulate connection refused
echo ""
echo "000"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"
}

@test "health-check exits 0 when health endpoint returns healthy" {
    create_env_file
    create_mock_curl_healthy

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
}

@test "health-check exits 1 when health endpoint returns unhealthy" {
    create_env_file
    create_mock_curl_unhealthy

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 1 ]
    [ -f "$CLAUDIO_PATH/claudio.log" ]
    grep -q "unhealthy" "$CLAUDIO_PATH/claudio.log"
}

@test "health-check logs pending updates when non-zero" {
    create_env_file
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
echo '{"status":"healthy","checks":{"telegram_webhook":{"status":"ok","pending_updates":5}}}'
echo "200"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    [ -f "$CLAUDIO_PATH/claudio.log" ]
    grep -q "pending updates: 5" "$CLAUDIO_PATH/claudio.log"
}

@test "health-check fails when env file is missing" {
    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 1 ]
}

@test "health-check fails when server is not running" {
    create_env_file
    create_mock_curl_server_down

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 1 ]
    grep -q "Could not connect to server" "$CLAUDIO_PATH/claudio.log"
}

@test "health-check uses PORT from service.env" {
    cat > "$CLAUDIO_PATH/service.env" << 'EOF'
PORT="9999"
EOF

    # Mock curl that checks the port
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
if [[ "$*" == *":9999/health"* ]]; then
    echo '{"status":"healthy","checks":{}}'
    echo "200"
else
    echo "wrong port"
    echo "500"
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
}

@test "health-check uses default PORT 8421 when not set" {
    cat > "$CLAUDIO_PATH/service.env" << 'EOF'
TELEGRAM_BOT_TOKEN="test"
EOF

    # Mock curl that checks the port
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
if [[ "$*" == *":8421/health"* ]]; then
    echo '{"status":"healthy","checks":{}}'
    echo "200"
else
    echo "wrong port"
    echo "500"
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
}

# --- Tests for expanded health checks ---

@test "log rotation rotates files exceeding max size" {
    create_env_file
    create_mock_curl_healthy

    # Set low threshold so rotation triggers
    export LOG_MAX_SIZE=100

    # Create a log file larger than 100 bytes
    dd if=/dev/zero of="$CLAUDIO_PATH/test.log" bs=200 count=1 2>/dev/null

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    # Original should be gone, .1 should exist
    [ ! -f "$CLAUDIO_PATH/test.log" ]
    [ -f "$CLAUDIO_PATH/test.log.1" ]
}

@test "log rotation does not rotate small files" {
    create_env_file
    create_mock_curl_healthy

    export LOG_MAX_SIZE=10485760

    echo "small" > "$CLAUDIO_PATH/tiny.log"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    [ -f "$CLAUDIO_PATH/tiny.log" ]
    [ ! -f "$CLAUDIO_PATH/tiny.log.1" ]
}

@test "disk usage check passes when under threshold" {
    create_env_file
    create_mock_curl_healthy

    # Mock df to return low usage
    cat > "$BATS_TEST_TMPDIR/bin/df" << 'EOF'
#!/bin/bash
echo "Filesystem     1K-blocks    Used Available Use% Mounted on"
echo "/dev/sda1       30000000 3000000  27000000  10% /"
echo "/dev/sdb1      200000000  100000 199900000   1% /mnt/ssd"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/df"

    export DISK_USAGE_THRESHOLD=90

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    # Should NOT have disk warning in log
    ! grep -q "Disk usage high" "$CLAUDIO_PATH/claudio.log" 2>/dev/null
}

@test "disk usage check warns when over threshold" {
    create_env_file
    create_mock_curl_healthy

    cat > "$BATS_TEST_TMPDIR/bin/df" << 'EOF'
#!/bin/bash
echo "Filesystem     1K-blocks    Used Available Use% Mounted on"
echo "/dev/sda1       30000000 28000000   2000000  95% /"
echo "/dev/sdb1      200000000  100000 199900000   1% /mnt/ssd"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/df"

    export DISK_USAGE_THRESHOLD=90

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    grep -q "Disk usage high" "$CLAUDIO_PATH/claudio.log"
}

@test "backup freshness passes with recent backup" {
    create_env_file
    create_mock_curl_healthy

    # Create a fake backup directory with a recent timestamp
    local backup_root="$BATS_TEST_TMPDIR/claudio-backups/hourly"
    mkdir -p "$backup_root"
    local ts
    ts=$(date '+%Y-%m-%d_%H%M')
    mkdir -p "$backup_root/$ts"
    ln -s "$backup_root/$ts" "$backup_root/latest"

    export BACKUP_DEST="$BATS_TEST_TMPDIR"
    export BACKUP_MAX_AGE=7200

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    ! grep -q "Backup stale" "$CLAUDIO_PATH/claudio.log" 2>/dev/null
}

@test "backup freshness warns with old backup" {
    create_env_file
    create_mock_curl_healthy

    # Create a fake backup directory with an old timestamp
    local backup_root="$BATS_TEST_TMPDIR/claudio-backups/hourly"
    mkdir -p "$backup_root"
    mkdir -p "$backup_root/2020-01-01_0000"
    ln -s "$backup_root/2020-01-01_0000" "$backup_root/latest"

    export BACKUP_DEST="$BATS_TEST_TMPDIR"
    export BACKUP_MAX_AGE=7200

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    grep -q "Backup stale" "$CLAUDIO_PATH/claudio.log"
}

@test "backup freshness passes when no backup dir exists" {
    create_env_file
    create_mock_curl_healthy

    export BACKUP_DEST="$BATS_TEST_TMPDIR/nonexistent"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    ! grep -q "Backup stale" "$CLAUDIO_PATH/claudio.log" 2>/dev/null
}


# --- Tests for log analysis ---

# Helper: write log lines with timestamps relative to now
write_recent_log() {
    local offset_secs="${1:-0}"
    shift
    local ts
    if [[ "$(uname)" == "Darwin" ]]; then
        ts=$(date -v-"${offset_secs}"S '+%Y-%m-%d %H:%M:%S')
    else
        ts=$(date -d "-${offset_secs} seconds" '+%Y-%m-%d %H:%M:%S')
    fi
    printf '[%s] %s\n' "$ts" "$*" >> "$CLAUDIO_PATH/claudio.log"
}

@test "log analysis detects ERROR lines in recent logs" {
    create_env_file
    create_mock_curl_healthy
    export LOG_CHECK_WINDOW=300
    export LOG_ALERT_COOLDOWN=0

    write_recent_log 10 "[server] ERROR: Something went wrong"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    # Alert should contain the error — check via the alert stamp being created
    [ -f "$CLAUDIO_PATH/.last_log_alert" ]
}

@test "log analysis ignores old log entries" {
    create_env_file
    create_mock_curl_healthy
    export LOG_CHECK_WINDOW=300
    export LOG_ALERT_COOLDOWN=0

    # Write an error line from 10 minutes ago (outside 5min window)
    : > "$CLAUDIO_PATH/claudio.log"
    write_recent_log 600 "[server] ERROR: Old problem"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    # No alert stamp should be created
    [ ! -f "$CLAUDIO_PATH/.last_log_alert" ]
}

@test "log analysis detects rapid server restarts" {
    create_env_file
    create_mock_curl_healthy
    export LOG_CHECK_WINDOW=300
    export LOG_ALERT_COOLDOWN=0

    write_recent_log 60 "[server] Starting Claudio server on port 8421..."
    write_recent_log 50 "[server] Starting Claudio server on port 8421..."
    write_recent_log 40 "[server] Starting Claudio server on port 8421..."
    write_recent_log 30 "[server] Starting Claudio server on port 8421..."

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    [ -f "$CLAUDIO_PATH/.last_log_alert" ]
}

@test "log analysis ignores health-check connection errors" {
    create_env_file
    create_mock_curl_healthy
    export LOG_CHECK_WINDOW=300
    export LOG_ALERT_COOLDOWN=0

    write_recent_log 10 "[health-check] ERROR: Could not connect to server on port 8421"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    # Should NOT alert since we filter out "Could not connect"
    [ ! -f "$CLAUDIO_PATH/.last_log_alert" ]
}

@test "log analysis respects cooldown" {
    create_env_file
    create_mock_curl_healthy
    export LOG_CHECK_WINDOW=300
    export LOG_ALERT_COOLDOWN=1800

    # Simulate recent alert
    printf '%s' "$(date +%s)" > "$CLAUDIO_PATH/.last_log_alert"

    write_recent_log 10 "[server] ERROR: Something went wrong"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    # The stamp should still have the original timestamp (not updated)
    local stamp_before stamp_after
    stamp_before=$(cat "$CLAUDIO_PATH/.last_log_alert")
    # Re-run to confirm it stays throttled
    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"
    stamp_after=$(cat "$CLAUDIO_PATH/.last_log_alert")
    [ "$stamp_before" = "$stamp_after" ]
}

@test "log analysis detects pre-flight warnings" {
    create_env_file
    create_mock_curl_healthy
    export LOG_CHECK_WINDOW=300
    export LOG_ALERT_COOLDOWN=0

    write_recent_log 30 "[claude] Pre-flight check is taking longer than expected"
    write_recent_log 25 "[claude] Pre-flight check is taking longer than expected"
    write_recent_log 20 "[claude] Pre-flight check is taking longer than expected"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    [ -f "$CLAUDIO_PATH/.last_log_alert" ]
}

@test "log analysis no alert when logs are clean" {
    create_env_file
    create_mock_curl_healthy
    export LOG_CHECK_WINDOW=300
    export LOG_ALERT_COOLDOWN=0

    write_recent_log 10 "[telegram] Received message from chat_id=123"
    write_recent_log 5 "[backup] Hourly backup created: /mnt/ssd/claudio-backups/hourly/2026-02-11_1400"

    run "$BATS_TEST_DIRNAME/../lib/health-check.sh"

    [ "$status" -eq 0 ]
    [ ! -f "$CLAUDIO_PATH/.last_log_alert" ]
}

@test "cron_install adds cron entry" {
    source "$BATS_TEST_DIRNAME/../lib/service.sh"

    cat > "$BATS_TEST_TMPDIR/bin/crontab" << 'EOF'
#!/bin/bash
if [ "$1" = "-l" ]; then
    cat "$HOME/.fake_crontab" 2>/dev/null || true
else
    cat > "$HOME/.fake_crontab"
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/crontab"

    run cron_install

    [ "$status" -eq 0 ]
    grep -q "health-check.sh" "$HOME/.fake_crontab"
    grep -q "claudio-health-check" "$HOME/.fake_crontab"
}

@test "cron_uninstall removes cron entry" {
    source "$BATS_TEST_DIRNAME/../lib/service.sh"

    cat > "$BATS_TEST_TMPDIR/bin/crontab" << 'EOF'
#!/bin/bash
if [ "$1" = "-l" ]; then
    cat "$HOME/.fake_crontab" 2>/dev/null || true
else
    cat > "$HOME/.fake_crontab"
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/crontab"

    echo "*/5 * * * * /path/to/health-check.sh # claudio-health-check" > "$HOME/.fake_crontab"

    run cron_uninstall

    [ "$status" -eq 0 ]
    ! grep -q "claudio-health-check" "$HOME/.fake_crontab"
}
