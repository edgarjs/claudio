#!/bin/bash

# Centralized logging for Claudio
# All log messages go to a single file with module prefix

CLAUDIO_LOG_FILE="${CLAUDIO_LOG_FILE:-$HOME/.claudio/claudio.log}"

# Log a message with module prefix
# Usage: log "module" "message"
# Example: log "server" "Starting on port 8421"
log() {
    local module="$1"
    shift
    local msg
    msg="[$(date '+%Y-%m-%d %H:%M:%S')] [$module] $*"

    # Ensure log directory exists
    mkdir -p "$(dirname "$CLAUDIO_LOG_FILE")"

    # Write to log file
    echo "$msg" >> "$CLAUDIO_LOG_FILE"
}

# Log an error message (same as log but marked as ERROR)
# Usage: log_error "module" "error message"
log_error() {
    local module="$1"
    shift
    log "$module" "ERROR: $*"
}
