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
    # Create a fake systemctl that just exits 0 (don't run the real one!)
    local mock_dir="$BATS_TEST_TMPDIR/mock_bin"
    mkdir -p "$mock_dir"
    printf '#!/bin/bash\necho "mock systemctl called with: $*"\n' > "$mock_dir/systemctl"
    chmod +x "$mock_dir/systemctl"
    export PATH="$mock_dir:$PATH"
    source "$PROJECT_DIR/lib/service.sh"
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
    # Create a fake systemctl so the real service doesn't restart
    local mock_dir="$BATS_TEST_TMPDIR/mock_bin"
    mkdir -p "$mock_dir"
    printf '#!/bin/bash\necho "mock systemctl called with: $*"\n' > "$mock_dir/systemctl"
    chmod +x "$mock_dir/systemctl"
    export PATH="$mock_dir:$PATH"
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

@test "systemctl wrapper: blocks 'reload-or-restart claudio' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user reload-or-restart claudio
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "systemctl wrapper: blocks 'try-restart claudio' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user try-restart claudio
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "systemctl wrapper: blocks 'restart claudio.service' inside webhook" {
    run "$SAFEGUARDS_DIR/systemctl" --user restart claudio.service
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "systemctl wrapper: blocks when service name appears before action" {
    run "$SAFEGUARDS_DIR/systemctl" --user claudio restart
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "launchctl wrapper: blocks when service name appears before action" {
    run "$SAFEGUARDS_DIR/launchctl" com.claudio.server stop
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "systemctl wrapper: allows 'status claudio' inside webhook" {
    # Mock real systemctl so passthrough doesn't hit the real one
    local mock_dir="$BATS_TEST_TMPDIR/mock_bin"
    mkdir -p "$mock_dir"
    printf '#!/bin/bash\necho "passthrough: $*"\n' > "$mock_dir/systemctl"
    chmod +x "$mock_dir/systemctl"
    export PATH="$SAFEGUARDS_DIR:$mock_dir:${PATH#*$SAFEGUARDS_DIR:}"
    run "$SAFEGUARDS_DIR/systemctl" --user status claudio
    [[ "$output" != *"BLOCKED"* ]]
}

@test "systemctl wrapper: allows 'daemon-reload' inside webhook" {
    local mock_dir="$BATS_TEST_TMPDIR/mock_bin"
    mkdir -p "$mock_dir"
    printf '#!/bin/bash\necho "passthrough: $*"\n' > "$mock_dir/systemctl"
    chmod +x "$mock_dir/systemctl"
    export PATH="$SAFEGUARDS_DIR:$mock_dir:${PATH#*$SAFEGUARDS_DIR:}"
    run "$SAFEGUARDS_DIR/systemctl" --user daemon-reload
    [[ "$output" != *"BLOCKED"* ]]
}

@test "systemctl wrapper: allows 'restart other-service' inside webhook" {
    local mock_dir="$BATS_TEST_TMPDIR/mock_bin"
    mkdir -p "$mock_dir"
    printf '#!/bin/bash\necho "passthrough: $*"\n' > "$mock_dir/systemctl"
    chmod +x "$mock_dir/systemctl"
    export PATH="$SAFEGUARDS_DIR:$mock_dir:${PATH#*$SAFEGUARDS_DIR:}"
    run "$SAFEGUARDS_DIR/systemctl" --user restart other-service
    [[ "$output" != *"BLOCKED"* ]]
}

@test "systemctl wrapper: passes through when CLAUDIO_WEBHOOK_ACTIVE unset" {
    unset CLAUDIO_WEBHOOK_ACTIVE
    # Create a fake real systemctl so the wrapper passes through to it (not the real one)
    local mock_dir="$BATS_TEST_TMPDIR/mock_bin"
    mkdir -p "$mock_dir"
    printf '#!/bin/bash\necho "passthrough: $*"\n' > "$mock_dir/systemctl"
    chmod +x "$mock_dir/systemctl"
    # Put mock AFTER safeguards so wrapper finds mock as "real" systemctl
    export PATH="$SAFEGUARDS_DIR:$mock_dir:${PATH#*$SAFEGUARDS_DIR:}"
    run "$SAFEGUARDS_DIR/systemctl" --user restart claudio
    [[ "$output" != *"BLOCKED"* ]]
    [[ "$output" == *"passthrough"* ]]
}

# ── Layer 3: launchctl PATH wrapper ──────────────────────────────

@test "launchctl wrapper: blocks 'stop com.claudio.server' inside webhook" {
    run "$SAFEGUARDS_DIR/launchctl" stop com.claudio.server
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "launchctl wrapper: blocks 'bootout' with claudio inside webhook" {
    run "$SAFEGUARDS_DIR/launchctl" bootout gui/501/com.claudio.server
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "launchctl wrapper: blocks 'unload' with claudio plist inside webhook" {
    run "$SAFEGUARDS_DIR/launchctl" unload ~/Library/LaunchAgents/com.claudio.server.plist
    [ "$status" -eq 1 ]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "launchctl wrapper: allows 'list' inside webhook" {
    local mock_dir="$BATS_TEST_TMPDIR/mock_bin"
    mkdir -p "$mock_dir"
    printf '#!/bin/bash\necho "passthrough: $*"\n' > "$mock_dir/launchctl"
    chmod +x "$mock_dir/launchctl"
    export PATH="$SAFEGUARDS_DIR:$mock_dir:${PATH#*$SAFEGUARDS_DIR:}"
    run "$SAFEGUARDS_DIR/launchctl" list
    [[ "$output" != *"BLOCKED"* ]]
}

@test "launchctl wrapper: passes through when CLAUDIO_WEBHOOK_ACTIVE unset" {
    unset CLAUDIO_WEBHOOK_ACTIVE
    # Create a fake real launchctl so the wrapper passes through to it
    local mock_dir="$BATS_TEST_TMPDIR/mock_bin"
    mkdir -p "$mock_dir"
    printf '#!/bin/bash\necho "passthrough: $*"\n' > "$mock_dir/launchctl"
    chmod +x "$mock_dir/launchctl"
    export PATH="$SAFEGUARDS_DIR:$mock_dir:${PATH#*$SAFEGUARDS_DIR:}"
    run "$SAFEGUARDS_DIR/launchctl" stop com.claudio.server
    [[ "$output" != *"BLOCKED"* ]]
    [[ "$output" == *"passthrough"* ]]
}

# ── Agent safeguards propagation ──────────────────────────────────

@test "agent_spawn code includes CLAUDIO_WEBHOOK_ACTIVE env var" {
    # Verify agent.sh passes CLAUDIO_WEBHOOK_ACTIVE in the env -i calls
    run grep -c 'CLAUDIO_WEBHOOK_ACTIVE=' "$PROJECT_DIR/lib/agent.sh"
    [ "$output" -ge 2 ]  # Should appear in both setsid and perl branches
}

@test "agent_spawn code prepends safeguards dir to PATH" {
    # Verify agent.sh builds agent_path with safeguards dir
    run grep 'safeguards_dir' "$PROJECT_DIR/lib/agent.sh"
    [ "$status" -eq 0 ]
    run grep 'agent_path=' "$PROJECT_DIR/lib/agent.sh"
    [ "$status" -eq 0 ]
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

# ── Systemd unit file template ──────────────────────────────────

@test "systemd template includes StartLimitIntervalSec" {
    run grep -c "StartLimitIntervalSec=" "$PROJECT_DIR/lib/service.sh"
    [ "$output" = "1" ]
}

@test "systemd template includes StartLimitBurst" {
    run grep -c "StartLimitBurst=" "$PROJECT_DIR/lib/service.sh"
    [ "$output" = "1" ]
}

@test "systemd template includes KillMode=mixed" {
    run grep -c "KillMode=mixed" "$PROJECT_DIR/lib/service.sh"
    [ "$output" = "1" ]
}

@test "systemd template does not include ExecStartPre (agents must survive restarts)" {
    run grep -c "ExecStartPre=" "$PROJECT_DIR/lib/service.sh"
    [ "$output" = "0" ]
}
