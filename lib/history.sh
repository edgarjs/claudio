#!/bin/bash

history_add() {
    local role="$1"
    local content="$2"
    # Escape content for JSON
    local escaped
    escaped=$(printf '%s' "$content" | jq -Rs '.')
    echo "{\"role\":\"${role}\",\"content\":${escaped}}" >> "$CLAUDIO_HISTORY_FILE"
    history_trim
}

history_trim() {
    local max_lines="${MAX_HISTORY_LINES:-100}"
    if [ -f "$CLAUDIO_HISTORY_FILE" ]; then
        local count
        count=$(wc -l < "$CLAUDIO_HISTORY_FILE")
        if [ "$count" -gt "$max_lines" ]; then
            local tmp
            tmp=$(tail -n "$max_lines" "$CLAUDIO_HISTORY_FILE")
            echo "$tmp" > "$CLAUDIO_HISTORY_FILE"
        fi
    fi
}

history_get_context() {
    if [ ! -f "$CLAUDIO_HISTORY_FILE" ]; then
        echo ""
        return
    fi
    local context="Here is the recent conversation history for context:\n\n"
    local has_history=false
    while IFS= read -r line; do
        local role content
        role=$(echo "$line" | jq -r '.role')
        content=$(echo "$line" | jq -r '.content')
        if [ "$role" = "user" ]; then
            context+="Human: ${content}\n\n"
        else
            context+="Assistant: ${content}\n\n"
        fi
        has_history=true
    done < "$CLAUDIO_HISTORY_FILE"
    if [ "$has_history" = true ]; then
        echo -e "$context"
    else
        echo ""
    fi
}
