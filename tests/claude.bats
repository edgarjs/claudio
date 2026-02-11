#!/usr/bin/env bats

# Tests for claude.sh — process group isolation

setup() {
    export TMPDIR="$BATS_TEST_TMPDIR"
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_DB_FILE="$BATS_TEST_TMPDIR/test.db"
    export CLAUDIO_LOG_FILE="$BATS_TEST_TMPDIR/claudio.log"
    export MODEL="sonnet"

    # Create a stub claude that just echoes its prompt
    mkdir -p "$BATS_TEST_TMPDIR/.local/bin"
    printf '#!/bin/sh\necho "response from claude"\n' > "$BATS_TEST_TMPDIR/.local/bin/claude"
    chmod +x "$BATS_TEST_TMPDIR/.local/bin/claude"
    export HOME="$BATS_TEST_TMPDIR"

    source "$BATS_TEST_DIRNAME/../lib/log.sh"
    source "$BATS_TEST_DIRNAME/../lib/db.sh"
    source "$BATS_TEST_DIRNAME/../lib/history.sh"

    db_init
    history_init

    source "$BATS_TEST_DIRNAME/../lib/claude.sh"
}

@test "claude_run captures output from setsid'd process" {
    run claude_run "hello"
    [ "$status" -eq 0 ]
    [[ "$output" == *"response from claude"* ]]
}

@test "claude_run survives child doing kill 0" {
    # Simulate a claude process whose bash tool runs kill 0.
    # With setsid, kill 0 only hits claude's process group, not the caller.
    printf '#!/bin/sh\necho "partial output"\nkill 0\necho "after kill"\n' \
        > "$BATS_TEST_TMPDIR/.local/bin/claude"
    chmod +x "$BATS_TEST_TMPDIR/.local/bin/claude"

    run claude_run "hello"
    # The caller (claude_run) should survive and return partial output
    [ "$status" -eq 0 ]
    [[ "$output" == *"partial output"* ]]
}

@test "claude_run survives child doing kill -TERM 0" {
    printf '#!/bin/sh\necho "before signal"\nkill -TERM 0\n' \
        > "$BATS_TEST_TMPDIR/.local/bin/claude"
    chmod +x "$BATS_TEST_TMPDIR/.local/bin/claude"

    run claude_run "hello"
    [ "$status" -eq 0 ]
    [[ "$output" == *"before signal"* ]]
}

@test "claude_run cleans up temp files" {
    run claude_run "hello"
    [ "$status" -eq 0 ]
    # No leftover temp files from claude_run (out_file, stderr_output)
    local leftover
    leftover=$(find "$BATS_TEST_TMPDIR" -maxdepth 1 -name "tmp.*" -type f 2>/dev/null | wc -l)
    [ "$leftover" -eq 0 ]
}

# Helper: call claude_run with setsid hidden to force perl POSIX::setsid fallback.
# Overrides the 'command' builtin so 'command -v setsid' returns false,
# without removing directories from PATH (which would break rm, chmod, etc.).
# Safe because 'run' executes in a subshell — the override doesn't leak.
_claude_run_perl_fallback() {
    command() {
        if [ "$1" = "-v" ] && [ "$2" = "setsid" ]; then
            return 1
        fi
        builtin command "$@"
    }
    claude_run "$@"
}

@test "perl fallback captures output when setsid is unavailable" {
    command -v perl > /dev/null 2>&1 || skip "perl not available"

    run _claude_run_perl_fallback "hello"
    [ "$status" -eq 0 ]
    [[ "$output" == *"response from claude"* ]]
}

@test "perl fallback survives child doing kill 0" {
    command -v perl > /dev/null 2>&1 || skip "perl not available"

    printf '#!/bin/sh\necho "partial output"\nkill 0\necho "after kill"\n' \
        > "$BATS_TEST_TMPDIR/.local/bin/claude"
    chmod +x "$BATS_TEST_TMPDIR/.local/bin/claude"

    run _claude_run_perl_fallback "hello"
    [ "$status" -eq 0 ]
    [[ "$output" == *"partial output"* ]]
}

@test "perl fallback survives child doing kill -TERM 0" {
    command -v perl > /dev/null 2>&1 || skip "perl not available"

    printf '#!/bin/sh\necho "before signal"\nkill -TERM 0\n' \
        > "$BATS_TEST_TMPDIR/.local/bin/claude"
    chmod +x "$BATS_TEST_TMPDIR/.local/bin/claude"

    run _claude_run_perl_fallback "hello"
    [ "$status" -eq 0 ]
    [[ "$output" == *"before signal"* ]]
}

@test "claude_run populates CLAUDE_NOTIFIER_MESSAGES from notifier log" {
    # Create a claude stub that writes to the notifier log (simulating MCP messages)
    cat > "$BATS_TEST_TMPDIR/.local/bin/claude" << 'STUB'
#!/bin/sh
# Use the env var exported by claude_run to find the notifier log
if [ -n "$CLAUDIO_NOTIFIER_LOG" ]; then
    printf '"working on it..."\n' >> "$CLAUDIO_NOTIFIER_LOG"
    printf '"almost done"\n' >> "$CLAUDIO_NOTIFIER_LOG"
fi
echo "final response"
STUB
    chmod +x "$BATS_TEST_TMPDIR/.local/bin/claude"

    # Use run + helper to avoid set -e/RETURN trap interactions on macOS bash
    _run_and_get_notifier() {
        claude_run "hello" >/dev/null
        printf '%s' "$CLAUDE_NOTIFIER_MESSAGES"
    }
    run _run_and_get_notifier
    [ "$status" -eq 0 ]
    [[ "$output" == *"[Notification: working on it...]"* ]]
    [[ "$output" == *"[Notification: almost done]"* ]]
}

@test "claude_run leaves CLAUDE_NOTIFIER_MESSAGES empty when no notifications" {
    claude_run "hello"
    [ -z "$CLAUDE_NOTIFIER_MESSAGES" ]
}

@test "claude_run populates CLAUDE_TOOL_SUMMARY from tool log" {
    # Create a claude stub that writes to the tool log (simulating PostToolUse hook)
    cat > "$BATS_TEST_TMPDIR/.local/bin/claude" << 'STUB'
#!/bin/sh
if [ -n "$CLAUDIO_TOOL_LOG" ]; then
    printf 'Read server.py\n' >> "$CLAUDIO_TOOL_LOG"
    printf 'Bash "bats tests/"\n' >> "$CLAUDIO_TOOL_LOG"
fi
echo "final response"
STUB
    chmod +x "$BATS_TEST_TMPDIR/.local/bin/claude"

    _run_and_get_tool_summary() {
        claude_run "hello" >/dev/null
        printf '%s' "$CLAUDE_TOOL_SUMMARY"
    }
    run _run_and_get_tool_summary
    [ "$status" -eq 0 ]
    [[ "$output" == *'[Tool: Read server.py]'* ]]
    [[ "$output" == *'[Tool: Bash "bats tests/"]'* ]]
}

@test "claude_run leaves CLAUDE_TOOL_SUMMARY empty when no tools used" {
    claude_run "hello"
    [ -z "$CLAUDE_TOOL_SUMMARY" ]
}

@test "claude_run deduplicates repeated tool log lines" {
    cat > "$BATS_TEST_TMPDIR/.local/bin/claude" << 'STUB'
#!/bin/sh
if [ -n "$CLAUDIO_TOOL_LOG" ]; then
    printf 'Read server.py\n' >> "$CLAUDIO_TOOL_LOG"
    printf 'Read server.py\n' >> "$CLAUDIO_TOOL_LOG"
    printf 'Read server.py\n' >> "$CLAUDIO_TOOL_LOG"
    printf 'Edit server.py\n' >> "$CLAUDIO_TOOL_LOG"
    printf 'Read claude.sh\n' >> "$CLAUDIO_TOOL_LOG"
fi
echo "final response"
STUB
    chmod +x "$BATS_TEST_TMPDIR/.local/bin/claude"

    _run_and_get_tool_summary() {
        claude_run "hello" >/dev/null
        printf '%s' "$CLAUDE_TOOL_SUMMARY"
    }
    run _run_and_get_tool_summary
    [ "$status" -eq 0 ]
    # Each unique line should appear exactly once
    [[ "$output" == *'[Tool: Read server.py]'* ]]
    [[ "$output" == *'[Tool: Edit server.py]'* ]]
    [[ "$output" == *'[Tool: Read claude.sh]'* ]]
    # Count occurrences of "Read server.py" — should be exactly 1
    local count
    count=$(echo "$output" | grep -c 'Read server.py' || true)
    [ "$count" -eq 1 ]
}

@test "post-tool-use hook summarizes Read tool" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "Read", "tool_input": {"file_path": "/home/pi/claudio/lib/server.py"}, "tool_output": "file contents..."}
JSON
    run cat "$log_file"
    [ "$output" = "Read server.py" ]
}

@test "post-tool-use hook summarizes Bash tool" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "Bash", "tool_input": {"command": "git status"}, "tool_output": "On branch main"}
JSON
    run cat "$log_file"
    [ "$output" = 'Bash "git status"' ]
}

@test "post-tool-use hook summarizes Grep tool" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "Grep", "tool_input": {"pattern": "function_name", "path": "lib/"}, "tool_output": "matches"}
JSON
    run cat "$log_file"
    [ "$output" = 'Grep "function_name" in lib/' ]
}

@test "post-tool-use hook summarizes Task tool with subagent type and prompt" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "Task", "tool_input": {"subagent_type": "Explore", "prompt": "find auth"}, "tool_output": "Found auth uses JWT tokens in lib/auth.py"}
JSON
    run cat "$log_file"
    [[ "$output" == 'Task(Explore) "find auth"' ]]
}

@test "post-tool-use hook summarizes WebSearch tool without output" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "WebSearch", "tool_input": {"query": "python asyncio"}, "tool_output": "asyncio is a library for writing concurrent code"}
JSON
    run cat "$log_file"
    [[ "$output" == 'WebSearch "python asyncio"' ]]
}

@test "post-tool-use hook summarizes WebFetch tool without output" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "WebFetch", "tool_input": {"url": "https://docs.python.org/3/library/asyncio.html"}, "tool_output": "asyncio docs content"}
JSON
    run cat "$log_file"
    [[ "$output" == 'WebFetch docs.python.org' ]]
}

@test "post-tool-use hook summarizes Edit tool" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "Edit", "tool_input": {"file_path": "/home/pi/claudio/lib/claude.sh"}, "tool_output": "ok"}
JSON
    run cat "$log_file"
    [ "$output" = "Edit claude.sh" ]
}

@test "post-tool-use hook summarizes Write tool" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "Write", "tool_input": {"file_path": "/home/pi/claudio/lib/hooks/post-tool-use.py"}, "tool_output": "ok"}
JSON
    run cat "$log_file"
    [ "$output" = "Write post-tool-use.py" ]
}

@test "post-tool-use hook summarizes Glob tool" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "Glob", "tool_input": {"pattern": "**/*.sh"}, "tool_output": "lib/claude.sh\nlib/telegram.sh"}
JSON
    run cat "$log_file"
    [ "$output" = 'Glob "**/*.sh"' ]
}

@test "post-tool-use hook returns tool name for unknown tools" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "TodoWrite", "tool_input": {}, "tool_output": "ok"}
JSON
    run cat "$log_file"
    [ "$output" = "TodoWrite" ]
}

@test "post-tool-use hook skips MCP tools" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << 'JSON'
{"tool_name": "mcp__claudio-tools__send_telegram_message", "tool_input": {}, "tool_output": "sent"}
JSON
    # File should not exist or be empty
    [ ! -s "$log_file" ]
}

@test "post-tool-use hook is no-op without CLAUDIO_TOOL_LOG" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"

    # Ensure env var is unset
    unset CLAUDIO_TOOL_LOG
    run python3 "$hook" << 'JSON'
{"tool_name": "Read", "tool_input": {"file_path": "/tmp/test.py"}, "tool_output": "content"}
JSON
    [ "$status" -eq 0 ]
}

@test "post-tool-use hook truncates long Task prompt" {
    local hook="$BATS_TEST_DIRNAME/../lib/hooks/post-tool-use.py"
    local log_file="$BATS_TEST_TMPDIR/tool.log"

    # Generate prompt longer than 80 chars
    local long_prompt
    long_prompt=$(python3 -c "print('x' * 120)")

    CLAUDIO_TOOL_LOG="$log_file" python3 "$hook" << JSON
{"tool_name": "Task", "tool_input": {"subagent_type": "Explore", "prompt": "$long_prompt"}, "tool_output": "some result"}
JSON
    local content
    content=$(cat "$log_file")
    # Should be truncated with "..."
    [[ "$content" == *"..."* ]]
    # Should not contain full 120 chars of prompt (line = Task(Explore) " + 80 + ..." = ~100)
    [ ${#content} -lt 110 ]
}

@test "claude_hooks_install creates settings.json with hook" {
    source "$BATS_TEST_DIRNAME/../lib/config.sh"

    # Ensure no prior settings file
    rm -f "$HOME/.claude/settings.json"
    mkdir -p "$HOME/.claude"

    run claude_hooks_install "/opt/claudio"
    [ "$status" -eq 0 ]
    [ -f "$HOME/.claude/settings.json" ]

    # Verify the hook was added
    run jq -r '.hooks.PostToolUse[0].hooks[0].command' "$HOME/.claude/settings.json"
    [ "$output" = "python3 /opt/claudio/lib/hooks/post-tool-use.py" ]
}

@test "claude_hooks_install preserves existing settings" {
    source "$BATS_TEST_DIRNAME/../lib/config.sh"
    mkdir -p "$HOME/.claude"

    # Pre-existing settings
    cat > "$HOME/.claude/settings.json" << 'JSON'
{"model": "opus", "env": {"FOO": "bar"}}
JSON

    run claude_hooks_install "/opt/claudio"
    [ "$status" -eq 0 ]

    # Original settings preserved
    run jq -r '.model' "$HOME/.claude/settings.json"
    [ "$output" = "opus" ]
    run jq -r '.env.FOO' "$HOME/.claude/settings.json"
    [ "$output" = "bar" ]

    # Hook added
    run jq -r '.hooks.PostToolUse[0].hooks[0].command' "$HOME/.claude/settings.json"
    [ "$output" = "python3 /opt/claudio/lib/hooks/post-tool-use.py" ]
}

@test "claude_hooks_install is idempotent" {
    source "$BATS_TEST_DIRNAME/../lib/config.sh"
    mkdir -p "$HOME/.claude"
    echo '{}' > "$HOME/.claude/settings.json"

    # Install twice
    claude_hooks_install "/opt/claudio"
    claude_hooks_install "/opt/claudio"

    # Should have exactly one PostToolUse entry, not two
    run jq '.hooks.PostToolUse | length' "$HOME/.claude/settings.json"
    [ "$output" = "1" ]
}

@test "claude_hooks_install preserves existing hooks" {
    source "$BATS_TEST_DIRNAME/../lib/config.sh"
    mkdir -p "$HOME/.claude"

    # Pre-existing hook for a different event
    cat > "$HOME/.claude/settings.json" << 'JSON'
{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo pre"}]}]}}
JSON

    run claude_hooks_install "/opt/claudio"
    [ "$status" -eq 0 ]

    # Original hook preserved
    run jq -r '.hooks.PreToolUse[0].hooks[0].command' "$HOME/.claude/settings.json"
    [ "$output" = "echo pre" ]

    # New hook added
    run jq -r '.hooks.PostToolUse[0].hooks[0].command' "$HOME/.claude/settings.json"
    [ "$output" = "python3 /opt/claudio/lib/hooks/post-tool-use.py" ]
}

@test "claude_run uses setsid on Linux" {
    # Verify that setsid is available and would be used
    if ! command -v setsid > /dev/null 2>&1; then
        skip "setsid not available"
    fi

    # Create a claude stub that reports its session ID vs parent's
    printf '#!/bin/sh\necho "sid=$(cat /proc/self/sessionid 2>/dev/null || ps -o sid= -p $$ 2>/dev/null)"\n' \
        > "$BATS_TEST_TMPDIR/.local/bin/claude"
    chmod +x "$BATS_TEST_TMPDIR/.local/bin/claude"

    run claude_run "hello"
    [ "$status" -eq 0 ]
    # Output should contain sid= (proving the stub ran)
    [[ "$output" == *"sid="* ]]
}
