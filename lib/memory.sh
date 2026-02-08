#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

# Memory system bash glue — invokes lib/memory.py
# Degrades gracefully if fastembed is not installed

export MEMORY_ENABLED="${MEMORY_ENABLED:-1}"

_memory_py() {
    local lib_dir
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    printf '%s/memory.py' "$lib_dir"
}

memory_init() {
    if [ "$MEMORY_ENABLED" != "1" ]; then
        return 0
    fi

    # If daemon is running, skip init (schema already initialized, model loaded)
    if [ -S "${CLAUDIO_PATH}/memory.sock" ]; then
        return 0
    fi

    # Verify fastembed is importable
    if ! python3 -c "import fastembed" 2>/dev/null; then
        log_warn "memory" "fastembed not installed — memory system disabled"
        log_warn "memory" "Install with: pip3 install --user fastembed"
        MEMORY_ENABLED=0
        export MEMORY_ENABLED
        return 0
    fi

    # --warmup flag triggers model download + ONNX session init.
    # Only used during 'claudio start' (once), not on every webhook.
    local -a init_args=(init)
    if [ "${1:-}" = "--warmup" ]; then
        init_args+=(--warmup)
    fi

    ( set -o pipefail; python3 "$(_memory_py)" "${init_args[@]}" 2>&1 | while IFS= read -r line; do log "memory" "$line"; done ) || {
        log_warn "memory" "Failed to initialize memory schema"
        MEMORY_ENABLED=0
        export MEMORY_ENABLED
    }
}

memory_retrieve() {
    local query="$1"
    local top_k="${2:-5}"

    if [ "$MEMORY_ENABLED" != "1" ]; then
        return 0
    fi

    if [ -z "$query" ]; then
        return 0
    fi

    local result
    result=$(python3 "$(_memory_py)" retrieve --query "$query" --top-k "$top_k") || {
        log_warn "memory" "Memory retrieval failed"
        return 0
    }

    if [ -n "$result" ]; then
        printf '%s' "$result"
    fi
}

memory_consolidate() {
    if [ "$MEMORY_ENABLED" != "1" ]; then
        return 0
    fi

    # Serialize concurrent consolidations to prevent races on
    # last_consolidated_id (e.g., two background consolidations from
    # overlapping webhook handlers). Non-blocking: skip if locked.
    # Uses mkdir for atomic locking (portable across Linux and macOS,
    # unlike flock which is not available on macOS).
    local lock_dir="${CLAUDIO_PATH}/consolidate.lock"
    if ! mkdir "$lock_dir" 2>/dev/null; then
        # Check if the lock holder is still alive via PID file
        local lock_pid_file="${lock_dir}/pid"
        local lock_pid=""
        [ -f "$lock_pid_file" ] && lock_pid=$(cat "$lock_pid_file" 2>/dev/null)
        if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
            log "memory" "Consolidation already running (PID $lock_pid), skipping"
            return 0
        fi
        # No PID file yet — lock holder is still starting up
        if [ -z "$lock_pid" ]; then
            log "memory" "Consolidation lock has no PID yet, skipping"
            return 0
        fi
        # Lock holder is dead — reclaim the lock
        log_warn "memory" "Removing stale consolidation lock (PID ${lock_pid} gone)"
        rm -rf "$lock_dir" 2>/dev/null || true
        if ! mkdir "$lock_dir" 2>/dev/null; then
            log "memory" "Consolidation already running, skipping"
            return 0
        fi
    fi
    # Write our PID so other processes can check if we're alive
    echo "$$" > "${lock_dir}/pid" 2>/dev/null
    # shellcheck disable=SC2064
    trap "rm -rf '$lock_dir' 2>/dev/null" RETURN
    python3 "$(_memory_py)" consolidate || {
        log_warn "memory" "Memory consolidation failed"
    }
}

memory_reconsolidate() {
    if [ "$MEMORY_ENABLED" != "1" ]; then
        return 0
    fi

    python3 "$(_memory_py)" reconsolidate || {
        log_warn "memory" "Memory reconsolidation failed"
        return 0
    }
}
