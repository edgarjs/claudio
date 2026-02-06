#!/usr/bin/env bats

setup() {
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_DB_FILE="$BATS_TEST_TMPDIR/test.db"
    export CLAUDIO_PROMPT_FILE="$BATS_TEST_TMPDIR/SYSTEM_PROMPT.md"
    export CLAUDIO_LOG_FILE="$BATS_TEST_TMPDIR/claudio.log"
    export AGENT_OUTPUT_DIR="$BATS_TEST_TMPDIR/agent_outputs"
    export AGENT_MAX_CONCURRENT=5
    export AGENT_DEFAULT_TIMEOUT=300
    export AGENT_POLL_INTERVAL=1
    export AGENT_CLEANUP_AGE=24

    # Source dependencies
    source "$BATS_TEST_DIRNAME/../lib/log.sh"
    source "$BATS_TEST_DIRNAME/../lib/db.sh"
    source "$BATS_TEST_DIRNAME/../lib/agent.sh"

    # Initialize database
    db_init
    agent_init
}

teardown() {
    # Kill any leftover agent processes spawned during tests
    if [ -d "$AGENT_OUTPUT_DIR" ]; then
        rm -rf "$AGENT_OUTPUT_DIR"
    fi
    rm -f "$CLAUDIO_DB_FILE"
}

# ==================== agent_init ====================

@test "agent_init creates agents table" {
    result=$(sqlite3 "$CLAUDIO_DB_FILE" ".tables")
    [[ "$result" == *"agents"* ]]
}

@test "agent_init creates agent_reports table" {
    result=$(sqlite3 "$CLAUDIO_DB_FILE" ".tables")
    [[ "$result" == *"agent_reports"* ]]
}

@test "agent_init creates indexes" {
    result=$(sqlite3 "$CLAUDIO_DB_FILE" ".indexes agents")
    [[ "$result" == *"idx_agents_parent"* ]]
    [[ "$result" == *"idx_agents_status"* ]]
}

@test "agent_init is idempotent" {
    agent_init
    agent_init
    result=$(sqlite3 "$CLAUDIO_DB_FILE" ".tables")
    [[ "$result" == *"agents"* ]]
}

# ==================== _agent_gen_id ====================

@test "_agent_gen_id generates unique IDs" {
    local id1 id2
    id1=$(_agent_gen_id)
    id2=$(_agent_gen_id)

    [[ "$id1" == agent_* ]]
    [[ "$id2" == agent_* ]]
    [[ "$id1" != "$id2" ]]
}

@test "_agent_gen_id format matches expected pattern" {
    local id
    id=$(_agent_gen_id)
    # Format: agent_YYYYMMDD_HHMMSS_HEXRANDOM
    [[ "$id" =~ ^agent_[0-9]{8}_[0-9]{6}_[0-9a-f]+$ ]]
}

# ==================== _agent_sql_escape ====================

@test "_agent_sql_escape escapes single quotes" {
    result=$(_agent_sql_escape "it's a test")
    [[ "$result" == "it''s a test" ]]
}

@test "_agent_sql_escape handles no quotes" {
    result=$(_agent_sql_escape "hello world")
    [[ "$result" == "hello world" ]]
}

@test "_agent_sql_escape handles multiple quotes" {
    result=$(_agent_sql_escape "it's a 'test' of 'quotes'")
    [[ "$result" == "it''s a ''test'' of ''quotes''" ]]
}

# ==================== _agent_db_update ====================

@test "_agent_db_update changes status" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt) VALUES ('test1', 'parent1', 'test prompt');"

    _agent_db_update "test1" "running" "" "" "" "1234"

    local status
    status=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT status FROM agents WHERE id='test1';")
    [[ "$status" == "running" ]]
}

@test "_agent_db_update sets started_at for running status" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt) VALUES ('test2', 'parent1', 'test prompt');"

    _agent_db_update "test2" "running" "" "" "" "1234"

    local started_at
    started_at=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT started_at FROM agents WHERE id='test2';")
    [[ -n "$started_at" ]]
}

@test "_agent_db_update sets completed_at for terminal statuses" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt) VALUES ('test3', 'parent1', 'test prompt');"

    _agent_db_update "test3" "completed" "result output" "" "0" ""

    local completed_at
    completed_at=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT completed_at FROM agents WHERE id='test3';")
    [[ -n "$completed_at" ]]
}

@test "_agent_db_update stores output and error" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt) VALUES ('test4', 'parent1', 'test prompt');"

    _agent_db_update "test4" "failed" "some output" "some error" "1" ""

    local output error
    output=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT output FROM agents WHERE id='test4';")
    error=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT error FROM agents WHERE id='test4';")
    [[ "$output" == "some output" ]]
    [[ "$error" == "some error" ]]
}

@test "_agent_db_update handles quotes in output" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt) VALUES ('test5', 'parent1', 'test prompt');"

    _agent_db_update "test5" "completed" "it's a 'test' result" "" "0" ""

    local output
    output=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT output FROM agents WHERE id='test5';")
    [[ "$output" == "it's a 'test' result" ]]
}

@test "_agent_db_update records PID" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt) VALUES ('test6', 'parent1', 'test prompt');"

    _agent_db_update "test6" "running" "" "" "" "9999"

    local pid
    pid=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT pid FROM agents WHERE id='test6';")
    [[ "$pid" == "9999" ]]
}

# ==================== _agent_resolve_claude ====================

@test "_agent_resolve_claude finds claude binary" {
    local result
    result=$(_agent_resolve_claude)
    # Should find it somewhere if claude is installed
    [[ -n "$result" ]]
    [[ -x "$result" ]]
}

# ==================== agent_spawn ====================

@test "agent_spawn requires parent_id" {
    run agent_spawn "" "test prompt"
    [[ "$status" -eq 1 ]]
}

@test "agent_spawn requires prompt" {
    run agent_spawn "parent1" ""
    [[ "$status" -eq 1 ]]
}

@test "agent_spawn enforces max concurrent limit" {
    export AGENT_MAX_CONCURRENT=2

    # Insert 2 running agents
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a1', 'p1', 'prompt1', 'running');"
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a2', 'p1', 'prompt2', 'running');"

    run agent_spawn "p1" "prompt3"
    [[ "$status" -eq 1 ]]
}

@test "agent_spawn allows agents for different parents" {
    export AGENT_MAX_CONCURRENT=2

    # Insert 2 running agents for parent1
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a1', 'p1', 'prompt1', 'running');"
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a2', 'p1', 'prompt2', 'running');"

    # Should succeed for a different parent
    local agent_id
    agent_id=$(agent_spawn "p2" "echo hello" "haiku" 5)
    [[ -n "$agent_id" ]]
    [[ "$agent_id" == agent_* ]]

    # Clean up spawned process
    local pid
    pid=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT pid FROM agents WHERE id='$agent_id';")
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    sleep 1
}

@test "agent_spawn inserts pending record" {
    local agent_id
    agent_id=$(agent_spawn "parent1" "echo test" "haiku" 5)

    # Small delay to avoid WAL lock contention from spawned agent
    sleep 1

    local count
    count=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM agents WHERE id='$agent_id';")
    [[ "$count" == "1" ]]

    local parent_id
    parent_id=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT parent_id FROM agents WHERE id='$agent_id';")
    [[ "$parent_id" == "parent1" ]]

    # Clean up
    local pid
    pid=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT pid FROM agents WHERE id='$agent_id';")
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    sleep 1
}

@test "agent_spawn uses default model and timeout" {
    local agent_id
    agent_id=$(agent_spawn "parent1" "echo test")

    # Small delay to avoid WAL lock contention from spawned agent
    sleep 1

    local model timeout_secs
    model=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT model FROM agents WHERE id='$agent_id';")
    timeout_secs=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT timeout_seconds FROM agents WHERE id='$agent_id';")
    [[ "$model" == "haiku" ]]
    [[ "$timeout_secs" == "300" ]]

    # Clean up
    local pid
    pid=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT pid FROM agents WHERE id='$agent_id';")
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    sleep 1
}

@test "agent_spawn handles quotes in prompt" {
    local agent_id
    agent_id=$(agent_spawn "parent1" "echo it's a \"test\"" "haiku" 5)
    [[ -n "$agent_id" ]]

    local prompt
    prompt=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT prompt FROM agents WHERE id='$agent_id';")
    [[ "$prompt" == *"it's"* ]]

    # Clean up
    local pid
    pid=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT pid FROM agents WHERE id='$agent_id';")
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    sleep 1
}

# ==================== agent_poll ====================

@test "agent_poll requires parent_id" {
    run agent_poll ""
    [[ "$status" -eq 1 ]]
}

@test "agent_poll returns empty for unknown parent" {
    result=$(agent_poll "nonexistent")
    [[ -z "$result" || "$result" == "[]" ]]
}

@test "agent_poll returns agent statuses" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, model) VALUES ('a1', 'p1', 'p1', 'running', 'haiku');"
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, model) VALUES ('a2', 'p1', 'p2', 'completed', 'sonnet');"

    result=$(agent_poll "p1")
    # Should be valid JSON
    echo "$result" | jq . > /dev/null 2>&1
    [[ "$result" == *"running"* ]]
    [[ "$result" == *"completed"* ]]
}

# ==================== agent_get_results ====================

@test "agent_get_results requires parent_id" {
    run agent_get_results ""
    [[ "$status" -eq 1 ]]
}

@test "agent_get_results returns only terminal agents" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, output) VALUES ('a1', 'p1', 'p1', 'running', NULL);"
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, output) VALUES ('a2', 'p1', 'p2', 'completed', 'result');"

    result=$(agent_get_results "p1")
    [[ "$result" == *"completed"* ]]
    [[ "$result" != *"running"* ]]
}

@test "agent_get_results includes failed and orphaned agents" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, output) VALUES ('a1', 'p1', 'p1', 'failed', 'error output');"
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, output) VALUES ('a2', 'p1', 'p2', 'orphaned', NULL);"
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, output) VALUES ('a3', 'p1', 'p3', 'timeout', NULL);"

    result=$(agent_get_results "p1")
    [[ "$result" == *"failed"* ]]
    [[ "$result" == *"orphaned"* ]]
    [[ "$result" == *"timeout"* ]]
}

# ==================== _agent_detect_orphans ====================

@test "_agent_detect_orphans marks dead PID as orphaned" {
    # Use a PID that definitely doesn't exist
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, pid) VALUES ('a1', 'p1', 'test', 'running', 99999);"

    _agent_detect_orphans "p1"

    local status
    status=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT status FROM agents WHERE id='a1';")
    [[ "$status" == "orphaned" ]]
}

@test "_agent_detect_orphans recovers output from files" {
    mkdir -p "$AGENT_OUTPUT_DIR"
    echo "recovered output" > "${AGENT_OUTPUT_DIR}/a1.out"
    echo "recovered error" > "${AGENT_OUTPUT_DIR}/a1.err"

    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, pid) VALUES ('a1', 'p1', 'test', 'running', 99999);"

    _agent_detect_orphans "p1"

    local status output
    status=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT status FROM agents WHERE id='a1';")
    output=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT output FROM agents WHERE id='a1';")
    [[ "$status" == "completed" ]]
    [[ "$output" == "recovered output" ]]

    # Output files should be cleaned up
    [[ ! -f "${AGENT_OUTPUT_DIR}/a1.out" ]]
    [[ ! -f "${AGENT_OUTPUT_DIR}/a1.err" ]]
}

@test "_agent_detect_orphans ignores live processes" {
    # Use our own PID which is definitely alive
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, pid) VALUES ('a1', 'p1', 'test', 'running', $$);"

    _agent_detect_orphans "p1"

    local status
    status=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT status FROM agents WHERE id='a1';")
    [[ "$status" == "running" ]]
}

# ==================== agent_recover ====================

@test "agent_recover returns unreported completed agents" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, output) VALUES ('a1', 'p1', 'test prompt', 'completed', 'result');"

    result=$(agent_recover)
    [[ -n "$result" ]]
    echo "$result" | jq . > /dev/null 2>&1
    [[ "$result" == *"a1"* ]]
    [[ "$result" == *"result"* ]]
}

@test "agent_recover excludes already-reported agents" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, output) VALUES ('a1', 'p1', 'test', 'completed', 'result');"
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agent_reports (agent_id) VALUES ('a1');"

    result=$(agent_recover)
    [[ -z "$result" || "$result" == "[]" ]]
}

@test "agent_recover returns empty when no unreported agents" {
    result=$(agent_recover)
    [[ -z "$result" ]]
}

# ==================== agent_mark_reported ====================

@test "agent_mark_reported inserts report records" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a1', 'p1', 'test', 'completed');"
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a2', 'p1', 'test', 'completed');"

    agent_mark_reported "a1,a2"

    local count
    count=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM agent_reports;")
    [[ "$count" == "2" ]]
}

@test "agent_mark_reported is idempotent" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a1', 'p1', 'test', 'completed');"

    agent_mark_reported "a1"
    agent_mark_reported "a1"

    local count
    count=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM agent_reports;")
    [[ "$count" == "1" ]]
}

@test "agent_mark_reported handles empty input" {
    run agent_mark_reported ""
    [[ "$status" -eq 0 ]]
}

# ==================== agent_cleanup ====================

@test "agent_cleanup removes old completed records" {
    # Insert a record with old timestamp
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, created_at) VALUES ('a1', 'p1', 'test', 'completed', datetime('now', '-48 hours'));"

    agent_cleanup 24

    local count
    count=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM agents;")
    [[ "$count" == "0" ]]
}

@test "agent_cleanup keeps recent records" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a1', 'p1', 'test', 'completed');"

    agent_cleanup 24

    local count
    count=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM agents;")
    [[ "$count" == "1" ]]
}

@test "agent_cleanup keeps running agents regardless of age" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, created_at) VALUES ('a1', 'p1', 'test', 'running', datetime('now', '-48 hours'));"

    agent_cleanup 24

    local count
    count=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM agents;")
    [[ "$count" == "1" ]]
}

@test "agent_cleanup cascades to agent_reports" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, created_at) VALUES ('a1', 'p1', 'test', 'completed', datetime('now', '-48 hours'));"
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agent_reports (agent_id) VALUES ('a1');"

    agent_cleanup 24

    local count
    count=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM agent_reports;")
    [[ "$count" == "0" ]]
}

@test "agent_cleanup rejects invalid max_age_hours" {
    run agent_cleanup "abc"
    [[ "$status" -eq 1 ]]

    run agent_cleanup "0"
    [[ "$status" -eq 1 ]]
}

@test "agent_cleanup removes stale output files" {
    mkdir -p "$AGENT_OUTPUT_DIR"
    touch -d "2 days ago" "${AGENT_OUTPUT_DIR}/agent_old.out"
    echo "content" > "${AGENT_OUTPUT_DIR}/agent_recent.out"

    agent_cleanup 24

    [[ ! -f "${AGENT_OUTPUT_DIR}/agent_old.out" ]]
    [[ -f "${AGENT_OUTPUT_DIR}/agent_recent.out" ]]

    rm -f "${AGENT_OUTPUT_DIR}/agent_recent.out"
}

# ==================== _agent_enforce_timeouts ====================

@test "_agent_enforce_timeouts marks overdue agents" {
    # Insert an agent that started a long time ago with a short timeout
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, pid, timeout_seconds, started_at)
         VALUES ('a1', 'p1', 'test', 'running', 99999, 1, datetime('now', '-10 seconds'));"

    _agent_enforce_timeouts "p1"

    local status
    status=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT status FROM agents WHERE id='a1';")
    [[ "$status" == "timeout" ]]
}

@test "_agent_enforce_timeouts ignores agents within timeout" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status, pid, timeout_seconds, started_at)
         VALUES ('a1', 'p1', 'test', 'running', $$, 3600, datetime('now'));"

    _agent_enforce_timeouts "p1"

    local status
    status=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT status FROM agents WHERE id='a1';")
    [[ "$status" == "running" ]]
}

# ==================== Status constraint validation ====================

@test "agents table rejects invalid status" {
    run sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a1', 'p1', 'test', 'invalid_status');"
    [[ "$status" -ne 0 ]]
}

@test "agents table accepts all valid statuses" {
    for s in pending running completed failed timeout orphaned; do
        sqlite3 "$CLAUDIO_DB_FILE" \
            "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a_${s}', 'p1', 'test', '$s');"
    done

    local count
    count=$(sqlite3 "$CLAUDIO_DB_FILE" "SELECT COUNT(*) FROM agents;")
    [[ "$count" == "6" ]]
}

# ==================== agent_wait (basic) ====================

@test "agent_wait returns immediately when no agents" {
    sqlite3 "$CLAUDIO_DB_FILE" \
        "INSERT INTO agents (id, parent_id, prompt, status) VALUES ('a1', 'p1', 'test', 'completed');"

    # Should return quickly (all agents already terminal)
    run timeout 5 bash -c "
        export CLAUDIO_PATH='$CLAUDIO_PATH'
        export CLAUDIO_DB_FILE='$CLAUDIO_DB_FILE'
        export CLAUDIO_LOG_FILE='$CLAUDIO_LOG_FILE'
        export AGENT_OUTPUT_DIR='$AGENT_OUTPUT_DIR'
        source '$BATS_TEST_DIRNAME/../lib/log.sh'
        source '$BATS_TEST_DIRNAME/../lib/db.sh'
        source '$BATS_TEST_DIRNAME/../lib/agent.sh'
        agent_wait 'p1' 1
    "
    [[ "$status" -eq 0 ]]
}

@test "agent_wait requires parent_id" {
    run agent_wait ""
    [[ "$status" -eq 1 ]]
}
