#!/usr/bin/env bats

setup() {
    # Use a temporary database for each test
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_DB_FILE="$BATS_TEST_TMPDIR/test.db"
    export MAX_HISTORY_LINES=5

    # Source the history module
    source "$BATS_TEST_DIRNAME/../lib/history.sh"

    # Initialize
    history_init
}

teardown() {
    rm -f "$CLAUDIO_DB_FILE"
}

@test "history_init creates database" {
    [[ -f "$CLAUDIO_DB_FILE" ]]
}

@test "history_add stores messages" {
    history_add "user" "Test message"

    count=$(db_count)
    [[ "$count" == "1" ]]
}

@test "history_add auto-trims to MAX_HISTORY_LINES" {
    # MAX_HISTORY_LINES is set to 5 in setup
    for i in {1..10}; do
        history_add "user" "Message $i"
    done

    count=$(db_count)
    [[ "$count" == "5" ]]

    # Should have messages 6-10, not 1-5
    # Use line-based check to avoid "Message 1" matching inside "Message 10"
    result=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT content FROM messages ORDER BY id;" | tr '\n' ',')
    [[ "$result" != *"Message 1,"* ]]
    [[ "$result" == *"Message 10"* ]]
}

@test "history_get_context returns formatted history" {
    history_add "user" "Hello"
    history_add "assistant" "Hi there"

    result=$(history_get_context)

    [[ "$result" == *"H: Hello"* ]]
    [[ "$result" == *"A: Hi there"* ]]
}

@test "history_trim respects MAX_HISTORY_LINES" {
    export MAX_HISTORY_LINES=3

    for i in {1..5}; do
        db_add "user" "Message $i"
    done

    history_trim

    count=$(db_count)
    [[ "$count" == "3" ]]
}
