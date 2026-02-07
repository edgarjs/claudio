#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

# Memory system bash glue — invokes lib/memory.py
# Degrades gracefully if fastembed is not installed

MEMORY_ENABLED="${MEMORY_ENABLED:-1}"

_memory_py() {
    local lib_dir
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    printf '%s/memory.py' "$lib_dir"
}

memory_init() {
    if [ "$MEMORY_ENABLED" != "1" ]; then
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
        # Recover stale locks (>30 min old) from crashed processes
        local lock_mtime
        lock_mtime=$(stat -c%Y "$lock_dir" 2>/dev/null || stat -f%m "$lock_dir" 2>/dev/null || echo 0)
        local now
        now=$(date +%s)
        if [ $((now - lock_mtime)) -gt 1800 ]; then
            log_warn "memory" "Removing stale consolidation lock (age: $((now - lock_mtime))s)"
            rmdir "$lock_dir" 2>/dev/null || true
            if ! mkdir "$lock_dir" 2>/dev/null; then
                log "memory" "Consolidation already running, skipping"
                return 0
            fi
        else
            log "memory" "Consolidation already running, skipping"
            return 0
        fi
    fi
    # shellcheck disable=SC2064
    trap "rmdir '$lock_dir' 2>/dev/null" RETURN
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
