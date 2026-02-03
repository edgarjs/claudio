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
    # Use heredoc with sed for quote escaping to handle all special characters safely
    sqlite3 "$CLAUDIO_DB_FILE" <<SQL
INSERT INTO messages (role, content) VALUES ('$role', '$(echo "$content" | sed "s/'/''/g")');
SQL
}

db_get_context() {
    local limit="${1:-100}"
    local context="Here is the recent conversation history for context:\n\n"
    local has_history=false

    while IFS='|' read -r role content; do
        if [ -n "$role" ]; then
            if [ "$role" = "user" ]; then
                context+="H: ${content}\n\n"
            else
                context+="A: ${content}\n\n"
            fi
            has_history=true
        fi
    done < <(printf "SELECT role, content FROM (SELECT role, content, id FROM messages ORDER BY id DESC LIMIT %d) ORDER BY id ASC;\n" "$limit" | sqlite3 "$CLAUDIO_DB_FILE")

    if [ "$has_history" = true ]; then
        echo -e "$context"
    else
        echo ""
    fi
}

db_trim() {
    local max_rows="${1:-100}"
    printf "DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT %d);\n" "$max_rows" | sqlite3 "$CLAUDIO_DB_FILE"
}

db_clear() {
    sqlite3 "$CLAUDIO_DB_FILE" "DELETE FROM messages;"
}

db_count() {
    sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM messages;"
}
