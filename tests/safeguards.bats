#!/usr/bin/env bats

# Tests for self-restart protection (defense in depth):
# 1. service_restart() function guard
# 2. claudio restart command guard
# 3. PATH wrapper safeguards for systemctl/launchctl

setup() {
    SAFEGUARDS_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../lib/safeguards" && pwd)"
    PROJECT_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CLAUDIO_WEBHOOK_ACTIVE="1"
    export PATH="$SAFEGUARDS_DIR:$PATH"
}

teardown() {
    unset CLAUDIO_WEBHOOK_ACTIVE
}

# ── Layer 1: service_restart function guard ────────────────────────

@test "service_restart: blocked inside webhook handler" {
    source "$PROJECT_DIR/lib/service.sh"
    run service_restart
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "service_restart: allowed outside webhook handler" {
    unset CLAUDIO_WEBHOOK_ACTIVE
    source "$PROJECT_DIR/lib/service.sh"
    # Can't actually restart, so just verify it doesn't block
    # It will fail because systemctl isn't available in test, but NOT with BLOCKED
    run service_restart
    [[ "$output" != *"BLOCKED"* ]]
}

# ── Layer 2: claudio restart command guard ─────────────────────────

@test "claudio restart: blocked inside webhook handler" {
    run "$PROJECT_DIR/claudio" restart
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "claudio restart: allowed outside webhook handler" {
    unset CLAUDIO_WEBHOOK_ACTIVE
    # Will fail because systemctl isn't available, but NOT with BLOCKED
    run "$PROJECT_DIR/claudio" restart
    [[ "$output" != *"BLOCKED"* ]]
}

# ── Layer 3: systemctl PATH wrapper ───────────────────────────────

@test "systemctl wrapper: blocks 'restart claudio' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user restart claudio
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "systemctl wrapper: blocks 'stop claudio' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user stop claudio
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "systemctl wrapper: blocks 'kill claudio' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user kill claudio
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "systemctl wrapper: blocks 'restart claudio.service' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user restart claudio.service
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "systemctl wrapper: allows 'status claudio' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user status claudio
    [[ "$output" != *"BLOCKED"* ]]
}

@test "systemctl wrapper: allows 'daemon-reload' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user daemon-reload
    [[ "$output" != *"BLOCKED"* ]]
}

@test "systemctl wrapper: allows 'restart other-service' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user restart other-service
    [[ "$output" != *"BLOCKED"* ]]
}

@test "systemctl wrapper: passes through when CLAUDIO_WEBHOOK_ACTIVE unset" {
    unset CLAUDIO_WEBHOOK_ACTIVE
    run "$SAFEGUARDS_DIR/systemctl" --user restart claudio
    [[ "$output" != *"BLOCKED"* ]]
}

# ── Layer 3: launchctl PATH wrapper ──────────────────────────────

@test "launchctl wrapper: blocks 'stop com.claudio.server' inside webhook" {
    run "$SAFEGUARDS_DIR/launchctl" stop com.claudio.server
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "launchctl wrapper: blocks 'unload' with claudio plist inside webhook" {
    run "$SAFEGUARDS_DIR/launchctl" unload ~/Library/LaunchAgents/com.claudio.server.plist
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "launchctl wrapper: allows 'list' inside webhook" {
    run "$SAFEGUARDS_DIR/launchctl" list
    [[ "$output" != *"BLOCKED"* ]]
}

@test "launchctl wrapper: passes through when CLAUDIO_WEBHOOK_ACTIVE unset" {
    unset CLAUDIO_WEBHOOK_ACTIVE
    run "$SAFEGUARDS_DIR/launchctl" stop com.claudio.server
    [[ "$output" != *"BLOCKED"* ]]
}

# ── PATH resolution ──────────────────────────────────────────────

@test "systemctl wrapper shadows real systemctl in PATH" {
    local wrapper
    wrapper=$(command -v systemctl)
    [ "$wrapper" = "$SAFEGUARDS_DIR/systemctl" ]
}

@test "launchctl wrapper shadows real launchctl in PATH" {
    local wrapper
    wrapper=$(command -v launchctl)
    [ "$wrapper" = "$SAFEGUARDS_DIR/launchctl" ]
}
