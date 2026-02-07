#!/bin/bash

# Parallel agent management for Claudio
# Spawns independent claude -p processes in detached sessions,
# tracks state in SQLite, and handles crash recovery.

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

AGENT_OUTPUT_DIR="${CLAUDIO_PATH}/agent_outputs"
AGENT_MAX_CONCURRENT="${AGENT_MAX_CONCURRENT:-5}"
AGENT_DEFAULT_TIMEOUT="${AGENT_DEFAULT_TIMEOUT:-300}"
AGENT_POLL_INTERVAL="${AGENT_POLL_INTERVAL:-5}"
AGENT_CLEANUP_AGE="${AGENT_CLEANUP_AGE:-24}"
AGENT_MAX_OUTPUT_BYTES="${AGENT_MAX_OUTPUT_BYTES:-524288}"  # 512KB max output
AGENT_DEFAULT_MODEL="${AGENT_DEFAULT_MODEL:-haiku}"
AGENT_MAX_GLOBAL_CONCURRENT="${AGENT_MAX_GLOBAL_CONCURRENT:-15}"
AGENT_MAX_CONTEXT_BYTES="${AGENT_MAX_CONTEXT_BYTES:-262144}"  # 256KB max context injection

# Model whitelist — only allow known Claude models
_AGENT_VALID_MODELS="haiku sonnet opus"
# Max timeout cap — 1 hour
_AGENT_MAX_TIMEOUT=3600

# Initialize the agents and agent_reports tables
agent_init() {
    # Set restrictive permissions on output dir and DB (HIGH #7)
    mkdir -p "$AGENT_OUTPUT_DIR"
    chmod 700 "$AGENT_OUTPUT_DIR"
    # Ensure DB file is created with restrictive permissions before any data is written
    if [ ! -f "$CLAUDIO_DB_FILE" ]; then
        touch "$CLAUDIO_DB_FILE"
    fi
    chmod 600 "$CLAUDIO_DB_FILE"

    _agent_sql "PRAGMA journal_mode=WAL;" > /dev/null
    _agent_sql <<'SQL'
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
    pid_start_time INTEGER,
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

# Cross-platform stat helpers (GNU vs BSD)
# Get file size in bytes
_agent_file_size() {
    stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || echo 0
}

# Get file modification time as epoch
_agent_file_mtime() {
    stat -c%Y "$1" 2>/dev/null || stat -f%m "$1" 2>/dev/null || echo 0
}

# Generate a unique agent ID
_agent_gen_id() {
    printf 'agent_%s_%s' "$(date '+%Y%m%d_%H%M%S')" "$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' ')"
}

# Escape content for safe SQL insertion (double single quotes, strip NUL bytes)
_agent_sql_escape() {
    printf '%s' "$1" | tr -d '\0' | sed "s/'/''/g"
}

# Execute SQL with retry on WAL lock contention (MEDIUM #9)
_agent_sql() {
    local retries=5
    local delay=0.1
    local err_file
    err_file=$(mktemp) || { log_error "agent" "_agent_sql: mktemp failed"; return 1; }
    chmod 600 "$err_file"
    local i
    for (( i = 1; i <= retries; i++ )); do
        if sqlite3 "$CLAUDIO_DB_FILE" "$@" 2>"$err_file"; then
            rm -f "$err_file"
            return 0
        fi
        local err
        err=$(cat "$err_file" 2>/dev/null)
        if [[ "$err" == *"locked"* ]] && [ "$i" -lt "$retries" ]; then
            # Add jitter: delay * (0.5 to 1.5) to avoid thundering herd
            local jitter
            jitter=$(awk "BEGIN{srand(); print $delay * (0.5 + rand())}")
            sleep "$jitter"
            delay=$(awk "BEGIN{print $delay * 2}")
        else
            printf '%s\n' "$err" >&2
            rm -f "$err_file"
            return 1
        fi
    done
    rm -f "$err_file"
    return 1
}

# Update agent status in the database. Uses manual SQL string escaping.
_agent_db_update() {
    local agent_id="$1"
    local status="$2"
    local output="$3"
    local error="$4"
    local exit_code="$5"
    local pid="$6"

    # Validate agent_id (alphanumeric + underscore only)
    if ! [[ "$agent_id" =~ ^[a-zA-Z0-9_]+$ ]]; then
        log_error "agent" "_agent_db_update: invalid agent_id '$agent_id'"
        return 1
    fi

    # Validate status
    case "$status" in
        pending|running|completed|failed|timeout|orphaned) ;;
        *) log_error "agent" "_agent_db_update: invalid status '$status'"; return 1 ;;
    esac

    # Build timestamp clause
    local ts_clause=""
    case "$status" in
        running)
            ts_clause=", started_at=CURRENT_TIMESTAMP"
            ;;
        completed|failed|timeout|orphaned)
            ts_clause=", completed_at=CURRENT_TIMESTAMP"
            ;;
    esac

    # Validate numeric fields
    if [ -n "$exit_code" ] && ! [[ "$exit_code" =~ ^-?[0-9]+$ ]]; then
        exit_code=""
    fi
    if [ -n "$pid" ] && ! [[ "$pid" =~ ^[0-9]+$ ]]; then
        pid=""
    fi

    local exit_clause=""
    [ -n "$exit_code" ] && exit_clause=", exit_code=$exit_code"
    local pid_clause=""
    [ -n "$pid" ] && pid_clause=", pid=$pid"

    # Use Python parameterized queries for output/error (user-controlled content)
    local db_py
    db_py="$(dirname "${BASH_SOURCE[0]}")/db.py"

    # Build SET clause: status and timestamps are validated above, safe to interpolate
    local set_clause="status='$status'${exit_clause}${pid_clause}${ts_clause}"

    # Dynamically build parameterized SQL for user-controlled fields
    local -a params
    local sql_params=""
    if [ -n "$output" ]; then
        sql_params+=", output=?"
        params+=("$output")
    fi
    if [ -n "$error" ]; then
        sql_params+=", error=?"
        params+=("$error")
    fi

    if [ ${#params[@]} -gt 0 ]; then
        python3 "$db_py" exec "$CLAUDIO_DB_FILE" \
            "UPDATE agents SET ${set_clause}${sql_params} WHERE id='$agent_id'" \
            "${params[@]}"
    else
        _agent_sql "UPDATE agents SET ${set_clause} WHERE id='$agent_id';"
    fi
}

# Resolve the absolute path to the claude binary
_agent_resolve_claude() {
    local home="${HOME:-}"
    if [ -z "$home" ]; then
        home=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6) || \
            home=$(dscl . -read "/Users/$(id -un)" NFSHomeDirectory 2>/dev/null | awk '{print $2}') || \
            home=$(eval echo "~")
    fi
    if [ -z "$home" ]; then
        return 1
    fi

    if [ -x "$home/.local/bin/claude" ]; then
        printf '%s' "$home/.local/bin/claude"
    elif [ -x "/opt/homebrew/bin/claude" ]; then
        printf '%s' "/opt/homebrew/bin/claude"
    elif [ -x "/usr/local/bin/claude" ]; then
        printf '%s' "/usr/local/bin/claude"
    elif [ -x "/usr/bin/claude" ]; then
        printf '%s' "/usr/bin/claude"
    else
        return 1
    fi
}

# Get process start time (epoch) for PID recycling detection (HIGH #6)
# Works on Linux (GNU date), macOS (BSD date), and WSL
_agent_pid_start_time() {
    local pid="$1"
    local lstart
    lstart=$(ps -p "$pid" -o lstart= 2>/dev/null) || return 1
    [ -z "$lstart" ] && return 1
    # Trim leading/trailing whitespace
    lstart=$(printf '%s' "$lstart" | xargs)
    # Convert lstart to epoch: try GNU date first, then BSD date
    # GNU: date -d "Thu Feb  5 14:30:00 2026" +%s
    # BSD: date -j -f "%a %b %d %T %Y" "Thu Feb  5 14:30:00 2026" +%s
    date -d "$lstart" +%s 2>/dev/null || date -j -f "%a %b %d %T %Y" "$lstart" +%s 2>/dev/null || return 1
}

# Check if a PID is alive AND matches the expected start time (HIGH #6)
_agent_pid_alive() {
    local pid="$1"
    local expected_start_time="$2"

    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi

    # If no expected start time recorded, fall back to kill -0 only
    if [ -z "$expected_start_time" ] || [ "$expected_start_time" = "0" ]; then
        return 0
    fi

    local actual_start_time
    actual_start_time=$(_agent_pid_start_time "$pid") || return 1  # can't verify start time, assume recycled (fail-closed)
    [ "$actual_start_time" = "$expected_start_time" ]
}

# Kill an agent's process group (HIGH #5)
_agent_kill() {
    local pid="$1"
    local expected_start_time="$2"

    if ! _agent_pid_alive "$pid" "$expected_start_time"; then
        return 0  # already dead
    fi

    # Kill the entire process group (negative PID)
    kill -TERM "-$pid" 2>/dev/null || true
    sleep 0.5
    if _agent_pid_alive "$pid" "$expected_start_time"; then
        kill -9 "-$pid" 2>/dev/null || true
    fi
}

# Signal handler for agent wrapper — kills child and writes partial results
_agent_wrapper_cleanup() {
    local agent_id="$1"
    local child_pid="$2"
    if [ -n "$child_pid" ]; then
        kill "$child_pid" 2>/dev/null || true
    fi
    _agent_db_update "$agent_id" "failed" "" "Wrapper received signal" "" ""
    exit 1
}

# Sanitize agent output to prevent prompt injection when injected into context
# Strips XML-like tags that could manipulate the parent Claude instance
_agent_sanitize_output() {
    local output="$1"
    printf '%s' "$output" | sed -E 's/<\/?[a-zA-Z_][a-zA-Z0-9_-]*[^>]*>/[agent output continues]/g'
}

# Agent wrapper — runs in a detached process (new session)
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

    # Record PID + start time for recycling detection
    local my_start_time
    my_start_time=$(_agent_pid_start_time "$$") || my_start_time="0"
    _agent_db_update "$agent_id" "running" "" "" "" "$$"
    _agent_sql "UPDATE agents SET pid_start_time=$my_start_time WHERE id='$agent_id';"

    # Handle SIGTERM gracefully: kill child claude process, write partial results
    local claude_pid=""
    trap '_agent_wrapper_cleanup "$agent_id" "$claude_pid"' TERM INT

    # Use predictable file paths based on agent_id (no EXIT trap — files survive
    # wrapper crash so agent_recover can read results from a dead wrapper)
    ( umask 077; mkdir -p "$AGENT_OUTPUT_DIR" )
    local out_file="${AGENT_OUTPUT_DIR}/${agent_id}.out"
    local err_file="${AGENT_OUTPUT_DIR}/${agent_id}.err"
    ( umask 077; touch "$out_file" "$err_file" )

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

    # Run claude with timeout (gtimeout on macOS via coreutils)
    # Falls back to running without timeout if neither is available
    local timeout_cmd=""
    if command -v timeout > /dev/null 2>&1; then
        timeout_cmd="timeout"
    elif command -v gtimeout > /dev/null 2>&1; then
        timeout_cmd="gtimeout"
    fi
    local exit_code=0
    if [ -n "$timeout_cmd" ]; then
        "$timeout_cmd" "$timeout_secs" "$claude_cmd" "${claude_args[@]}" \
            > "$out_file" 2> "$err_file" &
    else
        "$claude_cmd" "${claude_args[@]}" \
            > "$out_file" 2> "$err_file" &
    fi
    claude_pid=$!
    wait "$claude_pid" || exit_code=$?
    claude_pid=""

    # Determine final status
    local status="completed"
    if [ "$exit_code" -eq 124 ]; then
        status="timeout"
    elif [ "$exit_code" -ne 0 ]; then
        status="failed"
    fi

    # Read output files, truncating if too large (MEDIUM #10)
    local output="" error=""
    if [ -f "$out_file" ]; then
        local file_size
        file_size=$(_agent_file_size "$out_file")
        if [ "$file_size" -gt "$AGENT_MAX_OUTPUT_BYTES" ]; then
            output=$(head -c "$AGENT_MAX_OUTPUT_BYTES" "$out_file")
            output="${output}
[OUTPUT TRUNCATED: ${file_size} bytes exceeded ${AGENT_MAX_OUTPUT_BYTES} byte limit]"
        else
            output=$(cat "$out_file")
        fi
    fi
    if [ -f "$err_file" ]; then
        error=$(head -c "$AGENT_MAX_OUTPUT_BYTES" "$err_file")
    fi

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
    local model="${3:-$AGENT_DEFAULT_MODEL}"
    local timeout_secs="${4:-$AGENT_DEFAULT_TIMEOUT}"

    if [ -z "$parent_id" ] || [ -z "$prompt" ]; then
        log_error "agent" "agent_spawn: parent_id and prompt are required"
        return 1
    fi

    # Validate parent_id (alphanumeric + underscore only)
    if ! [[ "$parent_id" =~ ^[a-zA-Z0-9_]+$ ]]; then
        log_error "agent" "agent_spawn: invalid parent_id '$parent_id'"
        return 1
    fi

    # Validate model against whitelist
    local valid=false
    local m
    for m in $_AGENT_VALID_MODELS; do
        if [ "$model" = "$m" ]; then valid=true; break; fi
    done
    if [ "$valid" = "false" ]; then
        log_error "agent" "agent_spawn: invalid model '$model' (allowed: $_AGENT_VALID_MODELS)"
        return 1
    fi

    # Validate timeout (numeric, capped)
    if ! [[ "$timeout_secs" =~ ^[0-9]+$ ]]; then
        log_error "agent" "agent_spawn: invalid timeout '$timeout_secs'"
        return 1
    fi
    if [ "$timeout_secs" -gt "$_AGENT_MAX_TIMEOUT" ]; then
        log_warn "agent" "agent_spawn: timeout $timeout_secs capped to $_AGENT_MAX_TIMEOUT"
        timeout_secs="$_AGENT_MAX_TIMEOUT"
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

    # Atomic check + insert via Python parameterized queries (no SQL injection)
    local db_py
    db_py="$(dirname "${BASH_SOURCE[0]}")/db.py"
    local inserted
    inserted=$(python3 "$db_py" agent_insert "$CLAUDIO_DB_FILE" \
        "$agent_id" "$parent_id" "$prompt" "$model" "$timeout_secs" \
        "$AGENT_MAX_CONCURRENT" "$AGENT_MAX_GLOBAL_CONCURRENT" 2>&1)
    if [ "${inserted:-0}" -eq 0 ]; then
        log_error "agent" "Max concurrent agents ($AGENT_MAX_CONCURRENT) reached for parent $parent_id"
        return 1
    fi

    # Get the absolute path to this script for sourcing in the wrapper
    local agent_script
    agent_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/agent.sh"

    # Write prompt to a temp file to avoid shell escaping issues
    # Use umask to create with restrictive permissions atomically (no TOCTOU)
    (
        umask 077
        mkdir -p "$AGENT_OUTPUT_DIR"
    )
    local prompt_file="${AGENT_OUTPUT_DIR}/${agent_id}.prompt"
    ( umask 077; printf '%s' "$prompt" > "$prompt_file" )

    # Spawn detached process in a new session
    # All values passed via env vars — no shell interpolation of user content
    # Uses setsid on Linux, perl POSIX::setsid on macOS (perl is pre-installed)
    # Only pass safe env vars to agents (no secrets like TELEGRAM_BOT_TOKEN, ELEVENLABS_API_KEY)

    # Prepend safeguards dir to PATH so agents can't restart the service either
    local agent_path="$PATH"
    local safeguards_dir
    safeguards_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/safeguards" 2>/dev/null && pwd)" || true
    if [ -n "$safeguards_dir" ] && [ -d "$safeguards_dir" ]; then
        agent_path="${safeguards_dir}:${agent_path}"
    fi

    local wrapper_script='
        source "$_AGENT_SCRIPT"
        prompt=$(cat "$_AGENT_PROMPT_FILE")
        rm -f "$_AGENT_PROMPT_FILE"
        _agent_wrapper "$_AGENT_ID" "$prompt" "$_AGENT_MODEL" "$_AGENT_TIMEOUT" "$_AGENT_CLAUDE_CMD"
    '

    if command -v setsid > /dev/null 2>&1; then
        env -i \
            HOME="$HOME" PATH="$agent_path" TERM="${TERM:-}" \
            CLAUDIO_WEBHOOK_ACTIVE="${CLAUDIO_WEBHOOK_ACTIVE:-}" \
            CLAUDIO_PATH="$CLAUDIO_PATH" \
            CLAUDIO_DB_FILE="$CLAUDIO_DB_FILE" \
            CLAUDIO_PROMPT_FILE="$CLAUDIO_PROMPT_FILE" \
            CLAUDIO_LOG_FILE="$CLAUDIO_LOG_FILE" \
            AGENT_OUTPUT_DIR="$AGENT_OUTPUT_DIR" \
            AGENT_MAX_OUTPUT_BYTES="$AGENT_MAX_OUTPUT_BYTES" \
            _AGENT_ID="$agent_id" \
            _AGENT_PROMPT_FILE="$prompt_file" \
            _AGENT_MODEL="$model" \
            _AGENT_TIMEOUT="$timeout_secs" \
            _AGENT_CLAUDE_CMD="$claude_cmd" \
            _AGENT_SCRIPT="$agent_script" \
            nohup setsid bash -c "$wrapper_script" > /dev/null 2>&1 &
    else
        env -i \
            HOME="$HOME" PATH="$agent_path" TERM="${TERM:-}" \
            CLAUDIO_WEBHOOK_ACTIVE="${CLAUDIO_WEBHOOK_ACTIVE:-}" \
            CLAUDIO_PATH="$CLAUDIO_PATH" \
            CLAUDIO_DB_FILE="$CLAUDIO_DB_FILE" \
            CLAUDIO_PROMPT_FILE="$CLAUDIO_PROMPT_FILE" \
            CLAUDIO_LOG_FILE="$CLAUDIO_LOG_FILE" \
            AGENT_OUTPUT_DIR="$AGENT_OUTPUT_DIR" \
            AGENT_MAX_OUTPUT_BYTES="$AGENT_MAX_OUTPUT_BYTES" \
            _AGENT_ID="$agent_id" \
            _AGENT_PROMPT_FILE="$prompt_file" \
            _AGENT_MODEL="$model" \
            _AGENT_TIMEOUT="$timeout_secs" \
            _AGENT_CLAUDE_CMD="$claude_cmd" \
            _AGENT_SCRIPT="$agent_script" \
            nohup perl -e 'use POSIX qw(setsid); setsid(); exec @ARGV' -- \
                bash -c "$wrapper_script" > /dev/null 2>&1 &
    fi

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

    # Also check for orphaned agents while polling
    _agent_detect_orphans "$parent_id"

    local db_py
    db_py="$(dirname "${BASH_SOURCE[0]}")/db.py"
    python3 "$db_py" query_json "$CLAUDIO_DB_FILE" \
        "SELECT id, status, model FROM agents WHERE parent_id=? ORDER BY created_at" \
        "$parent_id"
}

# Get results for all completed agents of a parent
# Returns JSON array of {id, status, model, output, error, exit_code}
agent_get_results() {
    local parent_id="$1"

    if [ -z "$parent_id" ]; then
        log_error "agent" "agent_get_results: parent_id is required"
        return 1
    fi

    local db_py
    db_py="$(dirname "${BASH_SOURCE[0]}")/db.py"
    python3 "$db_py" query_json "$CLAUDIO_DB_FILE" \
        "SELECT id, status, model, output, error, exit_code FROM agents
         WHERE parent_id=?
         AND status IN ('completed', 'failed', 'timeout', 'orphaned')
         ORDER BY created_at" \
        "$parent_id"
}

# Detect and mark orphaned agents (running but PID dead)
_agent_detect_orphans() {
    local parent_id="${1:-}"

    local db_py
    db_py="$(dirname "${BASH_SOURCE[0]}")/db.py"

    local running
    if [ -n "$parent_id" ]; then
        running=$(python3 "$db_py" exec "$CLAUDIO_DB_FILE" \
            "SELECT id, pid, pid_start_time FROM agents WHERE status = 'running' AND parent_id=?" \
            "$parent_id")
    else
        running=$(python3 "$db_py" exec "$CLAUDIO_DB_FILE" \
            "SELECT id, pid, pid_start_time FROM agents WHERE status = 'running'")
    fi

    [ -z "$running" ] && return 0

    while IFS='|' read -r agent_id pid pid_start_time; do
        [ -z "$agent_id" ] && continue
        if [ -n "$pid" ] && ! _agent_pid_alive "$pid" "$pid_start_time"; then
            # Check if the wrapper wrote output files before dying
            local out_file="${AGENT_OUTPUT_DIR}/${agent_id}.out"
            local err_file="${AGENT_OUTPUT_DIR}/${agent_id}.err"
            local output="" error=""
            if [ -f "$out_file" ]; then
                local file_size
                file_size=$(_agent_file_size "$out_file")
                if [ "$file_size" -gt "$AGENT_MAX_OUTPUT_BYTES" ]; then
                    output=$(head -c "$AGENT_MAX_OUTPUT_BYTES" "$out_file")
                    output="${output}
[OUTPUT TRUNCATED: ${file_size} bytes exceeded ${AGENT_MAX_OUTPUT_BYTES} byte limit]"
                else
                    output=$(cat "$out_file")
                fi
                rm -f "$out_file"
            fi
            [ -f "$err_file" ] && error=$(head -c "$AGENT_MAX_OUTPUT_BYTES" "$err_file") && rm -f "$err_file"

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

    local db_py
    db_py="$(dirname "${BASH_SOURCE[0]}")/db.py"
    local last_typing=0
    local typing_interval=15

    while true; do
        # Check for orphans and enforce timeouts
        _agent_detect_orphans "$parent_id"
        _agent_enforce_timeouts "$parent_id"

        # Count non-terminal agents
        local pending_count
        pending_count=$(python3 "$db_py" exec "$CLAUDIO_DB_FILE" \
            "SELECT COUNT(*) FROM agents WHERE parent_id=? AND status IN ('pending', 'running')" \
            "$parent_id")

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

    local db_py
    db_py="$(dirname "${BASH_SOURCE[0]}")/db.py"

    # Find agents running longer than 2x their timeout (agent already has its own
    # timeout(1) wrapper, so 2x is a safety net for when the wrapper itself hangs)
    local overdue
    if [ -n "$parent_id" ]; then
        overdue=$(python3 "$db_py" exec "$CLAUDIO_DB_FILE" \
            "SELECT id, pid, pid_start_time, timeout_seconds FROM agents
             WHERE status = 'running' AND parent_id=?
             AND started_at IS NOT NULL
             AND (strftime('%s', 'now') - strftime('%s', started_at)) > (timeout_seconds * 2)" \
            "$parent_id")
    else
        overdue=$(python3 "$db_py" exec "$CLAUDIO_DB_FILE" \
            "SELECT id, pid, pid_start_time, timeout_seconds FROM agents
             WHERE status = 'running'
             AND started_at IS NOT NULL
             AND (strftime('%s', 'now') - strftime('%s', started_at)) > (timeout_seconds * 2)")
    fi

    [ -z "$overdue" ] && return 0

    while IFS='|' read -r agent_id pid pid_start_time timeout_secs; do
        [ -z "$agent_id" ] && continue
        if [ -n "$pid" ]; then
            _agent_kill "$pid" "$pid_start_time"
            log_warn "agent" "Force-killed agent $agent_id (PID $pid) — exceeded 2x timeout (${timeout_secs}s)"
        fi
        _agent_db_update "$agent_id" "timeout" "" "Exceeded 2x timeout safety net" "" ""
    done <<< "$overdue"
}

# Recover unreported agent results (crash recovery)
# Returns JSON of unreported results, or empty string
# Uses BEGIN EXCLUSIVE to prevent concurrent recoveries (HIGH #3)
agent_recover() {
    # Detect orphans across all agents (not scoped to a parent)
    _agent_detect_orphans

    # Atomically: find unreported agents, mark them as reported, return results.
    # BEGIN EXCLUSIVE prevents concurrent agent_recover from claiming same agents.
    local unreported
    unreported=$(_agent_sql "
        BEGIN EXCLUSIVE;
        CREATE TEMP TABLE IF NOT EXISTS _recover_batch (agent_id TEXT);
        DELETE FROM _recover_batch;
        INSERT INTO _recover_batch (agent_id)
            SELECT id FROM agents
            WHERE status IN ('completed', 'failed', 'orphaned', 'timeout')
            AND id NOT IN (SELECT agent_id FROM agent_reports);
        INSERT OR IGNORE INTO agent_reports (agent_id)
            SELECT agent_id FROM _recover_batch;
        COMMIT;
        SELECT json_group_array(json_object(
            'id', a.id,
            'prompt', a.prompt,
            'output', a.output,
            'status', a.status
        )) FROM agents a
        INNER JOIN _recover_batch b ON a.id = b.agent_id;
        DROP TABLE IF EXISTS _recover_batch;
    " 2>/dev/null)

    # json_group_array returns [null] when no rows match
    if [ -n "$unreported" ] && [ "$unreported" != "[]" ] && [ "$unreported" != "[null]" ]; then
        printf '%s' "$unreported"
    fi
}

# Mark agents as reported (so they don't show up in future recoveries)
agent_mark_reported() {
    local agent_ids="$1"  # comma-separated list of agent IDs

    if [ -z "$agent_ids" ]; then
        return 0
    fi

    local db_py
    db_py="$(dirname "${BASH_SOURCE[0]}")/db.py"

    local IFS=','
    for agent_id in $agent_ids; do
        # Trim whitespace
        agent_id=$(printf '%s' "$agent_id" | tr -d ' ')
        [ -z "$agent_id" ] && continue
        python3 "$db_py" exec "$CLAUDIO_DB_FILE" \
            "INSERT OR IGNORE INTO agent_reports (agent_id) VALUES (?)" \
            "$agent_id"
    done
}

# Throttled cleanup — only runs if last cleanup was >1 hour ago (MEDIUM #12)
agent_cleanup_if_needed() {
    local marker_file="${AGENT_OUTPUT_DIR}/.last_cleanup"
    if [ -f "$marker_file" ]; then
        local last_cleanup
        last_cleanup=$(_agent_file_mtime "$marker_file")
        local now
        now=$(date +%s)
        if [ $((now - last_cleanup)) -lt 3600 ]; then
            return 0  # cleaned up less than 1 hour ago
        fi
    fi
    agent_cleanup "$@"
    mkdir -p "$AGENT_OUTPUT_DIR"
    touch "$marker_file"
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
    # max_age_hours is validated as numeric above, safe for interpolation
    local deleted
    deleted=$(_agent_sql \
        "PRAGMA foreign_keys = ON;
         DELETE FROM agents WHERE created_at < datetime('now', '-$max_age_hours hours')
         AND status IN ('completed', 'failed', 'timeout', 'orphaned');
         SELECT changes();")

    # Clean up stale output files
    if [ -d "$AGENT_OUTPUT_DIR" ]; then
        local _max_age_secs=$((max_age_hours * 3600))
        local _now; _now=$(date +%s)
        find "$AGENT_OUTPUT_DIR" -name "agent_*" -type f 2>/dev/null | while IFS= read -r _f; do
            local _mtime
            _mtime=$(stat -c%Y "$_f" 2>/dev/null || stat -f%m "$_f" 2>/dev/null) || continue
            if (( _now - _mtime > _max_age_secs )); then
                rm -f "$_f" || log_warn "agent" "Failed to remove stale output file: $_f"
            fi
        done
    fi

    if [ "${deleted:-0}" -gt 0 ]; then
        log "agent" "Cleaned up $deleted old agent records (older than ${max_age_hours}h)"
    fi
}
