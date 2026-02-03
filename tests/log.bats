#!/usr/bin/env bats

setup() {
    export CLAUDIO_LOG_FILE="$BATS_TEST_TMPDIR/test.log"

    # Source the log module
    source "$BATS_TEST_DIRNAME/../lib/log.sh"
}

teardown() {
    rm -f "$CLAUDIO_LOG_FILE"
}

@test "log creates log file if it doesn't exist" {
    rm -f "$CLAUDIO_LOG_FILE"
    log "test" "Hello world" 2>/dev/null

    [[ -f "$CLAUDIO_LOG_FILE" ]]
}

@test "log writes message with timestamp and module" {
    log "mymodule" "Test message" 2>/dev/null

    result=$(cat "$CLAUDIO_LOG_FILE")
    [[ "$result" == *"[mymodule]"* ]]
    [[ "$result" == *"Test message"* ]]
    # Check timestamp format [YYYY-MM-DD HH:MM:SS]
    [[ "$result" =~ \[[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\] ]]
}

@test "log outputs to stderr" {
    result=$(log "test" "stderr test" 2>&1)

    [[ "$result" == *"[test]"* ]]
    [[ "$result" == *"stderr test"* ]]
}

@test "log_error prefixes message with ERROR" {
    log_error "test" "Something went wrong" 2>/dev/null

    result=$(cat "$CLAUDIO_LOG_FILE")
    [[ "$result" == *"ERROR: Something went wrong"* ]]
}

@test "log appends multiple messages" {
    log "mod1" "First message" 2>/dev/null
    log "mod2" "Second message" 2>/dev/null
    log "mod1" "Third message" 2>/dev/null

    line_count=$(wc -l < "$CLAUDIO_LOG_FILE")
    [[ $line_count -eq 3 ]]

    result=$(cat "$CLAUDIO_LOG_FILE")
    [[ "$result" == *"[mod1] First message"* ]]
    [[ "$result" == *"[mod2] Second message"* ]]
    [[ "$result" == *"[mod1] Third message"* ]]
}

@test "log handles special characters" {
    log "test" 'Message with $dollars and "quotes"' 2>/dev/null

    result=$(cat "$CLAUDIO_LOG_FILE")
    [[ "$result" == *'$dollars'* ]]
    [[ "$result" == *'"quotes"'* ]]
}

@test "log creates parent directories if needed" {
    export CLAUDIO_LOG_FILE="$BATS_TEST_TMPDIR/nested/dir/test.log"
    log "test" "Nested log" 2>/dev/null

    [[ -f "$CLAUDIO_LOG_FILE" ]]
}
