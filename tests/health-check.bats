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

    # Create mock curl in path
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    mkdir -p "$BATS_TEST_TMPDIR/bin"
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
