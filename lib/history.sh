#!/bin/bash

# shellcheck source=lib/db.sh
source "$(dirname "${BASH_SOURCE[0]}")/db.sh"

history_init() {
    db_init
}

history_add() {
    local role="$1"
    local content="$2"
    db_add "$role" "$content"
    history_trim
}

history_trim() {
    local max_rows="${MAX_HISTORY_LINES:-100}"
    db_trim "$max_rows"
}

history_get_context() {
    db_get_context "${MAX_HISTORY_LINES:-100}"
}
