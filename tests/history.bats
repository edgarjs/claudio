#!/usr/bin/env bats

setup() {
    # Use a temporary database for each test
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_DB_FILE="$BATS_TEST_TMPDIR/test.db"

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

@test "history_add does not trim messages" {
    for i in {1..10}; do
        history_add "user" "Message $i"
    done

    count=$(db_count)
    [[ "$count" == "10" ]]
}

@test "history_get_context returns formatted history" {
    history_add "user" "Hello"
    history_add "assistant" "Hi there"

    result=$(history_get_context)

    [[ "$result" == *"H: Hello"* ]]
    [[ "$result" == *"A: Hi there"* ]]
}
