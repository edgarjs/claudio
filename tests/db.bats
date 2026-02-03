#!/usr/bin/env bats

setup() {
    # Use a temporary database for each test
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_DB_FILE="$BATS_TEST_TMPDIR/test.db"

    # Source the db module
    source "$BATS_TEST_DIRNAME/../lib/db.sh"

    # Initialize database
    db_init
}

teardown() {
    rm -f "$CLAUDIO_DB_FILE"
}

@test "db_init creates messages table" {
    result=$(sqlite3 "$CLAUDIO_DB_FILE" ".tables")
    [[ "$result" == *"messages"* ]]
}

@test "db_add inserts a user message" {
    db_add "user" "Hello world"

    result=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT role, content FROM messages;")
    [[ "$result" == "user|Hello world" ]]
}

@test "db_add inserts an assistant message" {
    db_add "assistant" "Hi there"

    result=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT role, content FROM messages;")
    [[ "$result" == "assistant|Hi there" ]]
}

@test "db_add handles single quotes in content" {
    db_add "user" "It's a test with 'quotes'"

    result=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT content FROM messages;")
    [[ "$result" == "It's a test with 'quotes'" ]]
}

@test "db_add handles special characters" {
    db_add "user" 'Test with $dollars and `backticks` and "double quotes"'

    result=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT content FROM messages;")
    [[ "$result" == 'Test with $dollars and `backticks` and "double quotes"' ]]
}

@test "db_add handles multiline content" {
    db_add "user" "Line 1
Line 2
Line 3"

    count=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM messages;")
    [[ "$count" == "1" ]]

    content=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT content FROM messages;")
    [[ "$content" == *"Line 1"* ]]
    [[ "$content" == *"Line 2"* ]]
}

@test "db_count returns correct count" {
    [[ $(db_count) == "0" ]]

    db_add "user" "Message 1"
    [[ $(db_count) == "1" ]]

    db_add "assistant" "Message 2"
    [[ $(db_count) == "2" ]]
}

@test "db_clear removes all messages" {
    db_add "user" "Message 1"
    db_add "assistant" "Message 2"
    db_add "user" "Message 3"

    [[ $(db_count) == "3" ]]

    db_clear

    [[ $(db_count) == "0" ]]
}

@test "db_trim keeps only the most recent messages" {
    db_add "user" "Message 1"
    db_add "assistant" "Message 2"
    db_add "user" "Message 3"
    db_add "assistant" "Message 4"
    db_add "user" "Message 5"

    db_trim 3

    [[ $(db_count) == "3" ]]

    # Should keep the most recent messages (3, 4, 5)
    result=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT content FROM messages ORDER BY id;")
    [[ "$result" != *"Message 1"* ]]
    [[ "$result" != *"Message 2"* ]]
    [[ "$result" == *"Message 3"* ]]
}

@test "db_get_context returns empty string when no messages" {
    result=$(db_get_context)
    [[ -z "$result" ]]
}

@test "db_get_context formats conversation correctly" {
    db_add "user" "Hello"
    db_add "assistant" "Hi there"
    db_add "user" "How are you?"

    result=$(db_get_context)

    [[ "$result" == *"H: Hello"* ]]
    [[ "$result" == *"A: Hi there"* ]]
    [[ "$result" == *"H: How are you?"* ]]
}

@test "db_get_context respects limit parameter" {
    db_add "user" "Message 1"
    db_add "assistant" "Message 2"
    db_add "user" "Message 3"
    db_add "assistant" "Message 4"

    result=$(db_get_context 2)

    # Should only have the last 2 messages
    [[ "$result" != *"Message 1"* ]]
    [[ "$result" != *"Message 2"* ]]
    [[ "$result" == *"Message 3"* ]]
    [[ "$result" == *"Message 4"* ]]
}

@test "db_get_context returns messages in chronological order" {
    db_add "user" "First"
    db_add "assistant" "Second"
    db_add "user" "Third"

    result=$(db_get_context)

    # Verify order by checking positions
    first_pos=$(echo "$result" | grep -n "First" | cut -d: -f1)
    second_pos=$(echo "$result" | grep -n "Second" | cut -d: -f1)
    third_pos=$(echo "$result" | grep -n "Third" | cut -d: -f1)

    [[ $first_pos -lt $second_pos ]]
    [[ $second_pos -lt $third_pos ]]
}
