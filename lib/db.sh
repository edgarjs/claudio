#!/bin/bash

CLAUDIO_DB_FILE="${CLAUDIO_DB_FILE:-$CLAUDIO_PATH/history.db}"

db_init() {
    sqlite3 "$CLAUDIO_DB_FILE" <<'SQL'
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
SQL
}

db_add() {
    local role="$1"
    local content="$2"

    # Strict role validation to prevent SQL injection
    case "$role" in
        user|assistant) ;;
        *)
            echo "db_add: invalid role '$role'" >&2
            return 1
            ;;
    esac

    # Escape single quotes for SQL (SQLite standard: double the quotes)
    # shellcheck disable=SC2001  # sed is more reliable here for quote escaping
    local escaped_content
    escaped_content=$(printf '%s' "$content" | sed "s/'/''/g")

    # Pipe SQL via stdin to avoid ARG_MAX limit on large content
    # (printf is a bash builtin, not subject to execve ARG_MAX)
    if ! printf "INSERT INTO messages (role, content) VALUES ('%s', '%s');\n" "$role" "$escaped_content" | sqlite3 "$CLAUDIO_DB_FILE"; then
        echo "db_add: failed to insert message" >&2
        return 1
    fi
}

db_get_context() {
    local limit="${1:-100}"

    # Validate limit is a positive integer
    if ! [[ "$limit" =~ ^[0-9]+$ ]] || [ "$limit" -eq 0 ]; then
        echo "db_get_context: invalid limit '$limit'" >&2
        return 1
    fi

    # Use JSON output to safely handle multiline content
    local json_result
    json_result=$(printf '.mode json\nSELECT role, content FROM (SELECT role, content, id FROM messages ORDER BY id DESC LIMIT %d) ORDER BY id ASC;\n' "$limit" | sqlite3 "$CLAUDIO_DB_FILE")

    if [ -z "$json_result" ] || [ "$json_result" = "[]" ]; then
        echo ""
        return
    fi

    local context
    context=$(printf '%s' "$json_result" | jq -r '
        "Here is the recent conversation history for context:\n\n" +
        ([.[] | (if .role == "user" then "H" else "A" end) + ": " + .content] | join("\n\n")) +
        "\n\n"
    ')

    if [ -n "$context" ]; then
        printf '%s\n' "$context"
    else
        echo ""
    fi
}

db_clear() {
    if ! sqlite3 "$CLAUDIO_DB_FILE" "DELETE FROM messages;"; then
        echo "db_clear: failed to clear messages" >&2
        return 1
    fi
}

db_count() {
    if ! sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM messages;"; then
        echo "db_count: failed to count messages" >&2
        return 1
    fi
}
