#!/usr/bin/env bats

setup() {
    export BATS_TEST_TMPDIR="${BATS_TEST_TMPDIR:-/tmp/bats-$$}"
    mkdir -p "$BATS_TEST_TMPDIR"
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_LOG_FILE="$BATS_TEST_TMPDIR/claudio.log"
    export PORT=8080

    # Fast polling for tests
    export CLOUDFLARED_POLL_INTERVAL=0.05
    export CLOUDFLARED_RETRY_DELAY=0.1
    export CLOUDFLARED_MAX_ATTEMPTS=3

    # Create a mock cloudflared
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    mkdir -p "$BATS_TEST_TMPDIR/bin"

    # Source the server module
    source "$BATS_TEST_DIRNAME/../lib/server.sh"
}

teardown() {
    # Clean up any background processes
    if [ -n "$CLOUDFLARED_PID" ]; then
        kill $CLOUDFLARED_PID 2>/dev/null || true
    fi
    rm -rf "$BATS_TEST_TMPDIR"
}

create_mock_cloudflared() {
    local behavior="$1"
    cat > "$BATS_TEST_TMPDIR/bin/cloudflared" << EOF
#!/bin/bash
case "$behavior" in
    success)
        echo "https://test-tunnel-abc123.trycloudflare.com" >&1
        sleep 10
        ;;
    delayed)
        sleep 0.2
        echo "https://delayed-tunnel-xyz.trycloudflare.com" >&1
        sleep 10
        ;;
    fail)
        echo "Connection failed" >&2
        sleep 10
        ;;
esac
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/cloudflared"
}

@test "cloudflared_start skips when TUNNEL_TYPE is not set" {
    unset TUNNEL_TYPE

    run cloudflared_start

    [ "$status" -eq 0 ]
    [[ "$(cat "$CLAUDIO_LOG_FILE")" == *"No tunnel configured"* ]]
}

@test "cloudflared_start detects ephemeral URL on first attempt" {
    export TUNNEL_TYPE="ephemeral"
    create_mock_cloudflared "success"

    # Mock claudio_save_env
    claudio_save_env() { :; }
    export -f claudio_save_env

    # Run in background and wait a bit
    cloudflared_start &
    local pid=$!
    sleep 0.3

    # Check that URL was detected
    [[ "$(cat "$CLAUDIO_LOG_FILE")" == *"Ephemeral tunnel URL: https://test-tunnel-abc123.trycloudflare.com"* ]]

    kill $pid 2>/dev/null || true
}

@test "cloudflared_start retries when URL not immediately available" {
    export TUNNEL_TYPE="ephemeral"
    create_mock_cloudflared "delayed"

    claudio_save_env() { :; }
    export -f claudio_save_env

    cloudflared_start &
    local pid=$!
    sleep 0.5

    [[ "$(cat "$CLAUDIO_LOG_FILE")" == *"Ephemeral tunnel URL: https://delayed-tunnel-xyz.trycloudflare.com"* ]]

    kill $pid 2>/dev/null || true
}

@test "cloudflared_start exits with error after max retries" {
    export TUNNEL_TYPE="ephemeral"
    create_mock_cloudflared "fail"

    claudio_save_env() { :; }
    export -f claudio_save_env

    run cloudflared_start

    [ "$status" -eq 1 ]
    [[ "$(cat "$CLAUDIO_LOG_FILE")" == *"ERROR: Failed to detect ephemeral tunnel URL after 3 attempts"* ]]
}

@test "cloudflared_start starts named tunnel without URL detection" {
    export TUNNEL_TYPE="named"
    export TUNNEL_NAME="my-tunnel"

    cat > "$BATS_TEST_TMPDIR/bin/cloudflared" << 'EOF'
#!/bin/bash
echo "Starting named tunnel" >&1
sleep 10
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/cloudflared"

    cloudflared_start &
    local pid=$!
    sleep 0.2

    [[ "$(cat "$CLAUDIO_LOG_FILE")" == *"Named tunnel 'my-tunnel' started"* ]]

    kill $pid 2>/dev/null || true
}

@test "cloudflared_start registers webhook when Telegram is configured" {
    export TUNNEL_TYPE="ephemeral"
    export TELEGRAM_BOT_TOKEN="test-token"
    export TELEGRAM_CHAT_ID="12345"
    export WEBHOOK_SECRET="secret123"
    create_mock_cloudflared "success"

    claudio_save_env() { :; }
    export -f claudio_save_env

    # Mock curl to capture the webhook registration
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
echo '{"ok":true,"result":true}'
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    cloudflared_start &
    local pid=$!
    sleep 0.3

    [[ "$(cat "$CLAUDIO_LOG_FILE")" == *"Registering Telegram webhook"* ]]
    [[ "$(cat "$CLAUDIO_LOG_FILE")" == *"Webhook registration:"* ]]

    kill $pid 2>/dev/null || true
}
