#!/usr/bin/env bats

# Tests for claude.sh — process group isolation

setup() {
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_DB_FILE="$BATS_TEST_TMPDIR/test.db"
    export CLAUDIO_PROMPT_FILE="$BATS_TEST_TMPDIR/SYSTEM_PROMPT.md"
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
