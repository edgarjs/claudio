#!/bin/bash

# Parallel agent management for Claudio
# Spawns independent claude --p processes via nohup + setsid,
# tracks state in SQLite, and handles crash recovery.

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

AGENT_OUTPUT_DIR="${CLAUDIO_PATH}/agent_outputs"
AGENT_MAX_CONCURRENT="${AGENT_MAX_CONCURRENT:-5}"
AGENT_DEFAULT_TIMEOUT="${AGENT_DEFAULT_TIMEOUT:-300}"
AGENT_POLL_INTERVAL="${AGENT_POLL_INTERVAL:-5}"
AGENT_CLEANUP_AGE="${AGENT_CLEANUP_AGE:-24}"

# Initialize the agents and agent_reports tables
agent_init() {
    sqlite3 "$CLAUDIO_DB_FILE" "PRAGMA journal_mode=WAL;" > /dev/null
    sqlite3 "$CLAUDIO_DB_FILE" <<'SQL'
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'completed', 'failed', 'timeout', 'orphaned')),
    prompt TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'haiku',
    output TEXT,
    error TEXT,
    exit_code INTEGER,
    pid INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    timeout_seconds INTEGER DEFAULT 300
);
CREATE INDEX IF NOT EXISTS idx_agents_parent ON agents(parent_id);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);

CREATE TABLE IF NOT EXISTS agent_reports (
    agent_id TEXT PRIMARY KEY,
    reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
);
SQL
}

# Generate a unique agent ID
_agent_gen_id() {
    printf 'agent_%s_%s' "$(date '+%Y%m%d_%H%M%S')" "$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' ')"
}

# Escape content for safe SQL insertion (double single quotes)
_agent_sql_escape() {
    printf '%s' "$1" | sed "s/'/''/g"
}

# Update agent status in the database
_agent_db_update() {
    local agent_id="$1"
    local status="$2"
    local output="$3"
    local error="$4"
    local exit_code="$5"
    local pid="$6"

    local escaped_output escaped_error
    escaped_output=$(_agent_sql_escape "$output")
    escaped_error=$(_agent_sql_escape "$error")

    local sets="status='$status'"

    if [ -n "$output" ]; then
        sets="${sets}, output='${escaped_output}'"
    fi
    if [ -n "$error" ]; then
        sets="${sets}, error='${escaped_error}'"
    fi
    if [ -n "$exit_code" ]; then
        sets="${sets}, exit_code=$exit_code"
    fi
    if [ -n "$pid" ]; then
        sets="${sets}, pid=$pid"
    fi

    case "$status" in
        running)
            sets="${sets}, started_at=CURRENT_TIMESTAMP"
            ;;
        completed|failed|timeout|orphaned)
            sets="${sets}, completed_at=CURRENT_TIMESTAMP"
            ;;
    esac

    sqlite3 "$CLAUDIO_DB_FILE" "UPDATE agents SET $sets WHERE id='$agent_id';"
}

# Resolve the absolute path to the claude binary
_agent_resolve_claude() {
    local home="${HOME:-}"
    if [ -z "$home" ]; then
        home=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6) || home=$(eval echo "~")
    fi
    if [ -z "$home" ]; then
        return 1
    fi

    if [ -x "$home/.local/bin/claude" ]; then
        printf '%s' "$home/.local/bin/claude"
    elif [ -x "/usr/local/bin/claude" ]; then
        printf '%s' "/usr/local/bin/claude"
    elif [ -x "/usr/bin/claude" ]; then
        printf '%s' "/usr/bin/claude"
    else
        return 1
    fi
}

# Agent wrapper — runs in a detached process (nohup + setsid)
# Arguments: agent_id prompt model timeout_secs claude_cmd
_agent_wrapper() {
    local agent_id="$1"
    local prompt="$2"
    local model="$3"
    local timeout_secs="$4"
    local claude_cmd="$5"

    # Re-source config so we have CLAUDIO_DB_FILE etc.
    # The wrapper runs in a new session, so env vars from the parent are
    # passed via the nohup invocation, but we need the functions too.
    local lib_dir
    lib_dir="$(dirname "${BASH_SOURCE[0]}")"

    # Update status to running + record PID
    _agent_db_update "$agent_id" "running" "" "" "" "$$"

    # Use predictable file paths based on agent_id (no EXIT trap — files survive
    # wrapper crash so agent_recover can read results from a dead wrapper)
    mkdir -p "$AGENT_OUTPUT_DIR"
    local out_file="${AGENT_OUTPUT_DIR}/${agent_id}.out"
    local err_file="${AGENT_OUTPUT_DIR}/${agent_id}.err"

    # Build claude args
    local -a claude_args=(
        --dangerously-skip-permissions
        --disable-slash-commands
        --model "$model"
        --no-chrome
        --no-session-persistence
        --permission-mode bypassPermissions
        -p "$prompt"
    )

    # Add fallback model if different from primary
    if [ "$model" != "haiku" ]; then
        claude_args+=(--fallback-model haiku)
    fi

    # Add system prompt if available
    if [ -f "$CLAUDIO_PROMPT_FILE" ]; then
        local system_prompt
        system_prompt=$(cat "$CLAUDIO_PROMPT_FILE")
        if [ -n "$system_prompt" ]; then
            claude_args+=(--append-system-prompt "$system_prompt")
        fi
    fi

    # Run claude with timeout
    local exit_code=0
    timeout "$timeout_secs" "$claude_cmd" "${claude_args[@]}" \
        > "$out_file" 2> "$err_file" || exit_code=$?

    # Determine final status
    local status="completed"
    if [ "$exit_code" -eq 124 ]; then
        status="timeout"
    elif [ "$exit_code" -ne 0 ]; then
        status="failed"
    fi

    # Read output files
    local output="" error=""
    [ -f "$out_file" ] && output=$(cat "$out_file")
    [ -f "$err_file" ] && error=$(cat "$err_file")

    # Write results to DB
    _agent_db_update "$agent_id" "$status" "$output" "$error" "$exit_code" ""

    # Clean up output files only after DB write succeeds
    rm -f "$out_file" "$err_file"

    log "agent" "Agent $agent_id finished with status=$status exit_code=$exit_code"
}

# Spawn a new agent process
# Usage: agent_spawn <parent_id> <prompt> [model] [timeout]
# Outputs the agent ID on success
agent_spawn() {
    local parent_id="$1"
    local prompt="$2"
    local model="${3:-haiku}"
    local timeout_secs="${4:-$AGENT_DEFAULT_TIMEOUT}"

    if [ -z "$parent_id" ] || [ -z "$prompt" ]; then
        log_error "agent" "agent_spawn: parent_id and prompt are required"
        return 1
    fi

    # Check max concurrent agents
    local running_count
    running_count=$(sqlite3 "$CLAUDIO_DB_FILE" \
        "SELECT COUNT(*) FROM agents WHERE parent_id='$(_agent_sql_escape "$parent_id")' AND status IN ('pending', 'running');")
    if [ "$running_count" -ge "$AGENT_MAX_CONCURRENT" ]; then
        log_error "agent" "Max concurrent agents ($AGENT_MAX_CONCURRENT) reached for parent $parent_id"
        return 1
    fi

    # Resolve claude binary path
    local claude_cmd
    claude_cmd=$(_agent_resolve_claude)
    if [ -z "$claude_cmd" ]; then
        log_error "agent" "claude binary not found"
        return 1
    fi

    # Generate unique ID
    local agent_id
    agent_id=$(_agent_gen_id)

    # Insert pending record
    local escaped_prompt escaped_parent
    escaped_prompt=$(_agent_sql_escape "$prompt")
    escaped_parent=$(_agent_sql_escape "$parent_id")
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, model, timeout_seconds) VALUES ('$agent_id', '$escaped_parent', '$escaped_prompt', '$model', $timeout_secs);"

    # Get the absolute path to this script for sourcing in the wrapper
    local agent_script
    agent_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/agent.sh"

    # Spawn detached process via nohup + setsid
    # The wrapper sources this file to get _agent_wrapper and its dependencies
    nohup setsid bash -c "
        export CLAUDIO_PATH='$CLAUDIO_PATH'
        export CLAUDIO_DB_FILE='$CLAUDIO_DB_FILE'
        export CLAUDIO_PROMPT_FILE='$CLAUDIO_PROMPT_FILE'
        export CLAUDIO_LOG_FILE='$CLAUDIO_LOG_FILE'
        export AGENT_OUTPUT_DIR='$AGENT_OUTPUT_DIR'
        source '$agent_script'
        _agent_wrapper '$agent_id' \"\$(cat)\" '$model' '$timeout_secs' '$claude_cmd'
    " <<< "$prompt" > /dev/null 2>&1 &

    log "agent" "Spawned agent $agent_id (model=$model, timeout=${timeout_secs}s, parent=$parent_id)"
    printf '%s' "$agent_id"
}

# Poll agent statuses for a given parent
# Returns JSON array of {id, status, model}
agent_poll() {
    local parent_id="$1"

    if [ -z "$parent_id" ]; then
        log_error "agent" "agent_poll: parent_id is required"
        return 1
    fi

    local escaped_parent
    escaped_parent=$(_agent_sql_escape "$parent_id")

    # Also check for orphaned agents while polling
    _agent_detect_orphans "$parent_id"

    sqlite3 "$CLAUDIO_DB_FILE" -json \
        "SELECT id, status, model FROM agents WHERE parent_id='$escaped_parent' ORDER BY created_at;"
}

# Get results for all completed agents of a parent
# Returns JSON array of {id, status, model, output, error, exit_code}
agent_get_results() {
    local parent_id="$1"

    if [ -z "$parent_id" ]; then
        log_error "agent" "agent_get_results: parent_id is required"
        return 1
    fi

    local escaped_parent
    escaped_parent=$(_agent_sql_escape "$parent_id")

    sqlite3 "$CLAUDIO_DB_FILE" -json \
        "SELECT id, status, model, output, error, exit_code FROM agents
         WHERE parent_id='$escaped_parent'
         AND status IN ('completed', 'failed', 'timeout', 'orphaned')
         ORDER BY created_at;"
}

# Detect and mark orphaned agents (running but PID dead)
_agent_detect_orphans() {
    local parent_id="${1:-}"

    local where_clause="WHERE status = 'running'"
    if [ -n "$parent_id" ]; then
        local escaped_parent
        escaped_parent=$(_agent_sql_escape "$parent_id")
        where_clause="${where_clause} AND parent_id='$escaped_parent'"
    fi

    local running
    running=$(sqlite3 "$CLAUDIO_DB_FILE" \
        "SELECT id, pid FROM agents $where_clause;")

    [ -z "$running" ] && return 0

    while IFS='|' read -r agent_id pid; do
        [ -z "$agent_id" ] && continue
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            # Check if the wrapper wrote output files before dying
            local out_file="${AGENT_OUTPUT_DIR}/${agent_id}.out"
            local err_file="${AGENT_OUTPUT_DIR}/${agent_id}.err"
            local output="" error=""
            [ -f "$out_file" ] && output=$(cat "$out_file") && rm -f "$out_file"
            [ -f "$err_file" ] && error=$(cat "$err_file") && rm -f "$err_file"

            if [ -n "$output" ]; then
                _agent_db_update "$agent_id" "completed" "$output" "$error" "" ""
                log "agent" "Recovered output for agent $agent_id from files"
            else
                _agent_db_update "$agent_id" "orphaned" "" "Process disappeared (PID $pid)" "" ""
                log "agent" "Marked agent $agent_id as orphaned (PID $pid gone)"
            fi
        fi
    done <<< "$running"
}

# Blocking wait for all agents of a parent to reach terminal state
# Usage: agent_wait <parent_id> [poll_interval] [typing_chat_id]
agent_wait() {
    local parent_id="$1"
    local poll_interval="${2:-$AGENT_POLL_INTERVAL}"
    local typing_chat_id="${3:-}"

    if [ -z "$parent_id" ]; then
        log_error "agent" "agent_wait: parent_id is required"
        return 1
    fi

    local escaped_parent
    escaped_parent=$(_agent_sql_escape "$parent_id")
    local last_typing=0
    local typing_interval=15

    while true; do
        # Check for orphans and enforce timeouts
        _agent_detect_orphans "$parent_id"
        _agent_enforce_timeouts "$parent_id"

        # Count non-terminal agents
        local pending_count
        pending_count=$(sqlite3 "$CLAUDIO_DB_FILE" \
            "SELECT COUNT(*) FROM agents WHERE parent_id='$escaped_parent' AND status IN ('pending', 'running');")

        if [ "$pending_count" -eq 0 ]; then
            break
        fi

        # Send typing indicator (throttled)
        if [ -n "$typing_chat_id" ]; then
            local now
            now=$(date +%s)
            if [ $((now - last_typing)) -ge $typing_interval ]; then
                telegram_send_typing "$typing_chat_id"
                last_typing=$now
            fi
        fi

        sleep "$poll_interval"
    done
}

# Kill agents that have exceeded their timeout
_agent_enforce_timeouts() {
    local parent_id="${1:-}"

    local where_clause="WHERE status = 'running'"
    if [ -n "$parent_id" ]; then
        local escaped_parent
        escaped_parent=$(_agent_sql_escape "$parent_id")
        where_clause="${where_clause} AND parent_id='$escaped_parent'"
    fi

    # Find agents running longer than 2x their timeout (agent already has its own
    # timeout(1) wrapper, so 2x is a safety net for when the wrapper itself hangs)
    local overdue
    overdue=$(sqlite3 "$CLAUDIO_DB_FILE" \
        "SELECT id, pid, timeout_seconds FROM agents
         $where_clause
         AND started_at IS NOT NULL
         AND (strftime('%s', 'now') - strftime('%s', started_at)) > (timeout_seconds * 2);")

    [ -z "$overdue" ] && return 0

    while IFS='|' read -r agent_id pid timeout_secs; do
        [ -z "$agent_id" ] && continue
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
            log_warn "agent" "Force-killed agent $agent_id (PID $pid) — exceeded 2x timeout (${timeout_secs}s)"
        fi
        _agent_db_update "$agent_id" "timeout" "" "Exceeded 2x timeout safety net" "" ""
    done <<< "$overdue"
}

# Recover unreported agent results (crash recovery)
# Returns JSON of unreported results, or empty string
agent_recover() {
    # Detect orphans across all agents (not scoped to a parent)
    _agent_detect_orphans

    # Find completed agents that haven't been reported
    local unreported
    unreported=$(sqlite3 "$CLAUDIO_DB_FILE" -json \
        "SELECT id, prompt, output, status FROM agents
         WHERE status IN ('completed', 'failed', 'orphaned', 'timeout')
         AND id NOT IN (SELECT agent_id FROM agent_reports);" 2>/dev/null)

    if [ -n "$unreported" ] && [ "$unreported" != "[]" ]; then
        printf '%s' "$unreported"
    fi
}

# Mark agents as reported (so they don't show up in future recoveries)
agent_mark_reported() {
    local agent_ids="$1"  # comma-separated list of agent IDs

    if [ -z "$agent_ids" ]; then
        return 0
    fi

    # Build INSERT statements for each ID
    local sql=""
    local IFS=','
    for agent_id in $agent_ids; do
        # Trim whitespace
        agent_id=$(printf '%s' "$agent_id" | tr -d ' ')
        [ -z "$agent_id" ] && continue
        local escaped_id
        escaped_id=$(_agent_sql_escape "$agent_id")
        sql="${sql}INSERT OR IGNORE INTO agent_reports (agent_id) VALUES ('$escaped_id');"
    done

    if [ -n "$sql" ]; then
        sqlite3 "$CLAUDIO_DB_FILE" "$sql"
    fi
}

# Delete old agent records
# Usage: agent_cleanup [max_age_hours]
agent_cleanup() {
    local max_age_hours="${1:-$AGENT_CLEANUP_AGE}"

    # Validate input
    if ! [[ "$max_age_hours" =~ ^[0-9]+$ ]] || [ "$max_age_hours" -eq 0 ]; then
        log_error "agent" "agent_cleanup: invalid max_age_hours '$max_age_hours'"
        return 1
    fi

    # Delete old agent records and their reports
    local deleted
    deleted=$(sqlite3 "$CLAUDIO_DB_FILE" \
        "PRAGMA foreign_keys = ON;
         DELETE FROM agents WHERE created_at < datetime('now', '-$max_age_hours hours')
         AND status IN ('completed', 'failed', 'timeout', 'orphaned');
         SELECT changes();")

    # Clean up stale output files
    if [ -d "$AGENT_OUTPUT_DIR" ]; then
        find "$AGENT_OUTPUT_DIR" -name "agent_*" -mmin "+$((max_age_hours * 60))" -delete 2>/dev/null || true
    fi

    if [ "${deleted:-0}" -gt 0 ]; then
        log "agent" "Cleaned up $deleted old agent records (older than ${max_age_hours}h)"
    fi
}
