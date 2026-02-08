#!/usr/bin/env bats

# Tests for self-restart protection (defense in depth):
# 1. service_restart() function guard
# 2. claudio restart command guard
# 3. Claude Code PreToolUse safeguard hook (lib/safeguard-hook.sh)
# 4. hooks_install/hooks_uninstall in service.sh

setup() {
    PROJECT_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    HOOK_SCRIPT="$PROJECT_DIR/lib/safeguard-hook.sh"
    export CLAUDIO_WEBHOOK_ACTIVE="1"
}

teardown() {
    unset CLAUDIO_WEBHOOK_ACTIVE
}

# Helper: feed a fake PreToolUse JSON event to the hook script via jq
# (properly escapes quotes, backslashes, and special characters in commands)
_run_hook() {
    local command="$1"
    local input_file="$BATS_TEST_TMPDIR/hook_input.json"
    jq -n --arg cmd "$command" \
        '{hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{command:$cmd}}' \
        > "$input_file"
    run "$HOOK_SCRIPT" < "$input_file"
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

# ── Layer 3: PreToolUse safeguard hook ─────────────────────────────

# systemctl blocking

@test "hook: blocks 'systemctl restart claudio' inside webhook" {
    _run_hook "systemctl --user restart claudio"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
    [[ "$output" == *"BLOCKED"* ]]
}

@test "hook: blocks 'systemctl stop claudio' inside webhook" {
    _run_hook "systemctl --user stop claudio"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

@test "hook: blocks 'systemctl kill claudio' inside webhook" {
    _run_hook "systemctl --user kill claudio"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

@test "hook: blocks 'systemctl reload-or-restart claudio' inside webhook" {
    _run_hook "systemctl --user reload-or-restart claudio"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

@test "hook: blocks 'systemctl try-restart claudio' inside webhook" {
    _run_hook "systemctl --user try-restart claudio"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

@test "hook: blocks 'systemctl restart claudio.service' inside webhook" {
    _run_hook "systemctl --user restart claudio.service"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

@test "hook: blocks systemctl via absolute path" {
    _run_hook "/usr/bin/systemctl --user restart claudio"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

# systemctl allowing

@test "hook: allows 'systemctl status claudio' inside webhook" {
    _run_hook "systemctl --user status claudio"
    [ "$status" -eq 0 ]
    [[ "$output" != *"deny"* ]]
}

@test "hook: allows 'systemctl daemon-reload' inside webhook" {
    _run_hook "systemctl --user daemon-reload"
    [ "$status" -eq 0 ]
    [[ "$output" != *"deny"* ]]
}

@test "hook: allows 'systemctl restart other-service' inside webhook" {
    _run_hook "systemctl --user restart other-service"
    [ "$status" -eq 0 ]
    [[ "$output" != *"deny"* ]]
}

# launchctl blocking

@test "hook: blocks 'launchctl stop com.claudio.server' inside webhook" {
    _run_hook "launchctl stop com.claudio.server"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

@test "hook: blocks 'launchctl bootout' with claudio inside webhook" {
    _run_hook "launchctl bootout gui/501/com.claudio.server"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

@test "hook: blocks 'launchctl unload' with claudio plist inside webhook" {
    _run_hook "launchctl unload ~/Library/LaunchAgents/com.claudio.server.plist"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

@test "hook: blocks 'launchctl kickstart -k' with claudio inside webhook" {
    _run_hook "launchctl kickstart -k system/com.claudio.server"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deny"* ]]
}

# launchctl allowing

@test "hook: allows 'launchctl list' inside webhook" {
    _run_hook "launchctl list"
    [ "$status" -eq 0 ]
    [[ "$output" != *"deny"* ]]
}

# CLAUDIO_WEBHOOK_ACTIVE guard

@test "hook: passes through all commands when CLAUDIO_WEBHOOK_ACTIVE unset" {
    unset CLAUDIO_WEBHOOK_ACTIVE
    _run_hook "systemctl --user restart claudio"
    [ "$status" -eq 0 ]
    [[ "$output" != *"deny"* ]]
}

# ── Layer 3: hooks_install / hooks_uninstall ───────────────────────

@test "hooks_install: writes PreToolUse hook to settings" {
    local tmp_home="$BATS_TEST_TMPDIR/home"
    mkdir -p "$tmp_home/.claude"
    echo '{"statusLine": {"type": "command"}}' > "$tmp_home/.claude/settings.json"
    run env HOME="$tmp_home" bash -c "source '$PROJECT_DIR/lib/service.sh' && hooks_install"
    [ "$status" -eq 0 ]
    # Verify hook was added
    run jq -r '.hooks.PreToolUse[0].hooks[0].command' "$tmp_home/.claude/settings.json"
    [[ "$output" == *"safeguard-hook.sh"* ]]
    # Verify existing settings preserved
    run jq -r '.statusLine.type' "$tmp_home/.claude/settings.json"
    [ "$output" = "command" ]
}

@test "hooks_install: preserves other PreToolUse hooks" {
    local tmp_home="$BATS_TEST_TMPDIR/home_preserve"
    mkdir -p "$tmp_home/.claude"
    cat > "$tmp_home/.claude/settings.json" <<'EOF'
{
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Python",
                "hooks": [{"type": "command", "command": "/usr/bin/my-python-hook"}]
            }
        ]
    }
}
EOF
    run env HOME="$tmp_home" bash -c "source '$PROJECT_DIR/lib/service.sh' && hooks_install"
    [ "$status" -eq 0 ]
    # Verify our hook was added
    run jq -r '.hooks.PreToolUse[] | select(.matcher == "Bash") | .hooks[0].command' "$tmp_home/.claude/settings.json"
    [[ "$output" == *"safeguard-hook.sh"* ]]
    # Verify existing Python hook was preserved
    run jq -r '.hooks.PreToolUse[] | select(.matcher == "Python") | .hooks[0].command' "$tmp_home/.claude/settings.json"
    [ "$output" = "/usr/bin/my-python-hook" ]
}

@test "hooks_install: creates settings.json if missing" {
    local tmp_home="$BATS_TEST_TMPDIR/home_new"
    mkdir -p "$tmp_home"
    run env HOME="$tmp_home" bash -c "source '$PROJECT_DIR/lib/service.sh' && hooks_install"
    [ "$status" -eq 0 ]
    [ -f "$tmp_home/.claude/settings.json" ]
    run jq -r '.hooks.PreToolUse[0].matcher' "$tmp_home/.claude/settings.json"
    [ "$output" = "Bash" ]
}

@test "hooks_uninstall: removes safeguard hook from settings" {
    local tmp_home="$BATS_TEST_TMPDIR/home_rm"
    mkdir -p "$tmp_home/.claude"
    cat > "$tmp_home/.claude/settings.json" <<'EOF'
{"statusLine":{"type":"command"},"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"/path/to/safeguard-hook.sh"}]}]}}
EOF
    run env HOME="$tmp_home" bash -c "source '$PROJECT_DIR/lib/service.sh' && hooks_uninstall"
    [ "$status" -eq 0 ]
    # Hook should be gone
    run jq '.hooks' "$tmp_home/.claude/settings.json"
    [ "$output" = "null" ]
    # Other settings preserved
    run jq -r '.statusLine.type' "$tmp_home/.claude/settings.json"
    [ "$output" = "command" ]
}

@test "hooks_uninstall: no-op when no settings file exists" {
    local tmp_home="$BATS_TEST_TMPDIR/home_noop"
    mkdir -p "$tmp_home"
    run env HOME="$tmp_home" bash -c "source '$PROJECT_DIR/lib/service.sh' && hooks_uninstall"
    [ "$status" -eq 0 ]
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

