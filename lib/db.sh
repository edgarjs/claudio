#!/bin/bash

CLAUDIO_DB_FILE="${CLAUDIO_DB_FILE:-$CLAUDIO_PATH/history.db}"

_db_py() {
    python3 "$(dirname "${BASH_SOURCE[0]}")/db.py" "$@"
}

db_init() {
    _db_py init "$CLAUDIO_DB_FILE"
}

db_add() {
    local role="$1"
    local content="$2"

    if ! _db_py add "$CLAUDIO_DB_FILE" "$role" "$content"; then
        echo "db_add: failed to insert message" >&2
        return 1
    fi
}

db_get_context() {
    local limit="${1:-100}"
    _db_py get_context "$CLAUDIO_DB_FILE" "$limit"
}

db_clear() {
    if ! _db_py clear "$CLAUDIO_DB_FILE"; then
        echo "db_clear: failed to clear messages" >&2
        return 1
    fi
}

db_count() {
    if ! _db_py count "$CLAUDIO_DB_FILE"; then
        echo "db_count: failed to count messages" >&2
        return 1
    fi
}
