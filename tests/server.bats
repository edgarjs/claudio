#!/usr/bin/env bats

setup() {
    export BATS_TEST_TMPDIR="${BATS_TEST_TMPDIR:-/tmp/bats-$$}"
    mkdir -p "$BATS_TEST_TMPDIR"
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_LOG_FILE="$BATS_TEST_TMPDIR/claudio.log"
    export PORT=8080

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

@test "cloudflared_start skips when TUNNEL_TYPE is not set" {
    unset TUNNEL_TYPE
    unset TUNNEL_NAME

    run cloudflared_start

    [ "$status" -eq 0 ]
    [[ "$(cat "$CLAUDIO_LOG_FILE")" == *"No tunnel configured"* ]]
}

@test "cloudflared_start skips when TUNNEL_NAME is not set" {
    export TUNNEL_TYPE="named"
    unset TUNNEL_NAME

    run cloudflared_start

    [ "$status" -eq 0 ]
    [[ "$(cat "$CLAUDIO_LOG_FILE")" == *"No tunnel configured"* ]]
}

@test "cloudflared_start starts named tunnel" {
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
