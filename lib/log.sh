#!/bin/bash

# Centralized logging for Claudio
# All log messages go to a single file with module prefix

CLAUDIO_LOG_FILE="${CLAUDIO_LOG_FILE:-$HOME/.claudio/claudio.log}"
_LOG_DIR_INIT=false

print_error() {
    echo "‼️ Error: $*" >&2
}

print_warning() {
    echo "⚠️  Warning: $*"
}

print_success() {
    echo "✅ $*"
}

# Log a message with module prefix
# Usage: log "module" "message"
# Example: log "server" "Starting on port 8421"
log() {
    local module="$1"
    shift
    local msg
    msg="[$(date '+%Y-%m-%d %H:%M:%S')] [$module] $*"

    if ! $_LOG_DIR_INIT; then
        mkdir -p "$(dirname "$CLAUDIO_LOG_FILE")"
        _LOG_DIR_INIT=true
    fi

    printf '%s\n' "$msg" >> "$CLAUDIO_LOG_FILE"
}

# Log an error message (same as log but marked as ERROR)
# Usage: log_error "module" "error message"
log_error() {
    local module="$1"
    shift
    log "$module" "ERROR: $*"
}

# Log a warning message (same as log but marked as WARN)
# Usage: log_warn "module" "warning message"
log_warn() {
    local module="$1"
    shift
    log "$module" "WARN: $*"
}
