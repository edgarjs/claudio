#!/usr/bin/env bats

# Tests for lib/memory.sh — bash glue layer

setup() {
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_DB_FILE="$BATS_TEST_TMPDIR/test.db"
    export CLAUDIO_LOG_FILE="$BATS_TEST_TMPDIR/test.log"
    export MEMORY_ENABLED=1

    source "$BATS_TEST_DIRNAME/../lib/log.sh"
    source "$BATS_TEST_DIRNAME/../lib/memory.sh"
}

teardown() {
    rm -f "$CLAUDIO_DB_FILE" "$CLAUDIO_LOG_FILE"
}

@test "memory_init disables memory when fastembed is missing" {
    # Override python3 to simulate fastembed not installed
    python3() {
        if [[ "$*" == *"import fastembed"* ]]; then
            return 1
        fi
        command python3 "$@"
    }
    export -f python3

    MEMORY_ENABLED=1
    memory_init

    [[ "$MEMORY_ENABLED" == "0" ]]

    unset -f python3
}

@test "memory_init succeeds when memory is disabled" {
    MEMORY_ENABLED=0
    run memory_init
    [[ "$status" -eq 0 ]]
}

@test "memory_retrieve returns empty when disabled" {
    MEMORY_ENABLED=0
    result=$(memory_retrieve "test query")
    [[ -z "$result" ]]
}

@test "memory_retrieve returns empty with no query" {
    result=$(memory_retrieve "")
    [[ -z "$result" ]]
}

@test "memory_consolidate returns 0 when disabled" {
    MEMORY_ENABLED=0
    run memory_consolidate
    [[ "$status" -eq 0 ]]
}

@test "memory_reconsolidate returns 0 when disabled" {
    MEMORY_ENABLED=0
    run memory_reconsolidate
    [[ "$status" -eq 0 ]]
}

@test "_memory_py returns correct path" {
    result=$(_memory_py)
    [[ "$result" == *"lib/memory.py" ]]
    [[ -f "$result" ]]
}

@test "memory_init creates schema via python" {
    # Skip if fastembed not installed
    if ! python3 -c "import fastembed" 2>/dev/null; then
        skip "fastembed not installed"
    fi

    memory_init

    # Verify tables exist
    result=$(sqlite3 "$CLAUDIO_DB_FILE" ".tables" 2>/dev/null)
    [[ "$result" == *"episodic_memories"* ]]
    [[ "$result" == *"semantic_memories"* ]]
    [[ "$result" == *"procedural_memories"* ]]
    [[ "$result" == *"memory_accesses"* ]]
    [[ "$result" == *"memory_meta"* ]]
}

@test "memory_retrieve calls python with correct args" {
    # Skip if fastembed not installed
    if ! python3 -c "import fastembed" 2>/dev/null; then
        skip "fastembed not installed"
    fi

    # Init schema first
    python3 "$(_memory_py)" init 2>/dev/null

    # Retrieve should not fail even with empty DB
    run memory_retrieve "test query" 3
    [[ "$status" -eq 0 ]]
}
