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
    # Stop daemon if running — kill the process group (setsid gives it its own)
    if [ -n "${_daemon_pid:-}" ] && kill -0 "$_daemon_pid" 2>/dev/null; then
        kill -- -"$_daemon_pid" 2>/dev/null || kill "$_daemon_pid" 2>/dev/null || true
        # Wait briefly for clean shutdown
        for _ in 1 2 3 4 5; do
            kill -0 "$_daemon_pid" 2>/dev/null || break
            sleep 0.2
        done
        kill -9 -- -"$_daemon_pid" 2>/dev/null || kill -9 "$_daemon_pid" 2>/dev/null || true
    fi
    _daemon_pid=""
    rm -f "$CLAUDIO_DB_FILE" "$CLAUDIO_LOG_FILE" "$CLAUDIO_PATH/memory.sock"
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

# -- Daemon tests --

_start_test_daemon() {
    # Start daemon fully detached (setsid + closed FDs) so bats doesn't
    # wait for the background process to exit before finishing the test.
    setsid python3 "$(_memory_py)" serve \
        </dev/null >"$BATS_TEST_TMPDIR/daemon.log" 2>&1 &
    _daemon_pid=$!
    disown "$_daemon_pid" 2>/dev/null || true

    local deadline=$((SECONDS + 30))
    while [ $SECONDS -lt $deadline ]; do
        if [ -S "$CLAUDIO_PATH/memory.sock" ]; then
            return 0
        fi
        if ! kill -0 "$_daemon_pid" 2>/dev/null; then
            echo "Daemon exited early. Log:" >&2
            cat "$BATS_TEST_TMPDIR/daemon.log" >&2
            return 1
        fi
        sleep 0.2
    done
    echo "Daemon did not create socket within 30s" >&2
    return 1
}

@test "daemon starts and creates socket file" {
    if ! python3 -c "import fastembed" 2>/dev/null; then
        skip "fastembed not installed"
    fi

    _start_test_daemon
    [ -S "$CLAUDIO_PATH/memory.sock" ]
}

@test "daemon responds to ping" {
    if ! python3 -c "import fastembed" 2>/dev/null; then
        skip "fastembed not installed"
    fi

    _start_test_daemon

    # Send ping via Python (portable Unix socket client)
    result=$(python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect('$CLAUDIO_PATH/memory.sock')
s.sendall(b'{\"command\":\"ping\"}\n')
buf = b''
while b'\n' not in buf:
    chunk = s.recv(4096)
    if not chunk: break
    buf += chunk
s.close()
print(buf.decode().strip())
")
    [[ "$result" == *'"ok": true'* ]] || [[ "$result" == *'"ok":true'* ]]
}

@test "retrieve via daemon returns results" {
    if ! python3 -c "import fastembed" 2>/dev/null; then
        skip "fastembed not installed"
    fi

    _start_test_daemon

    # Retrieve with empty DB should succeed (empty result)
    run python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(10)
s.connect('$CLAUDIO_PATH/memory.sock')
req = json.dumps({'command': 'retrieve', 'query': 'test query', 'top_k': 3})
s.sendall(req.encode() + b'\n')
buf = b''
while b'\n' not in buf:
    chunk = s.recv(4096)
    if not chunk: break
    buf += chunk
s.close()
resp = json.loads(buf)
assert resp.get('ok') == True, f'Expected ok=True, got {resp}'
"
    [[ "$status" -eq 0 ]]
}

@test "fallback to local when daemon not running" {
    if ! python3 -c "import fastembed" 2>/dev/null; then
        skip "fastembed not installed"
    fi

    # No daemon running — socket doesn't exist
    rm -f "$CLAUDIO_PATH/memory.sock"

    # Init schema for local fallback
    CLAUDIO_DB_FILE="$CLAUDIO_DB_FILE" python3 "$(_memory_py)" init 2>/dev/null

    # Retrieve should work via local fallback
    run env CLAUDIO_PATH="$CLAUDIO_PATH" CLAUDIO_DB_FILE="$CLAUDIO_DB_FILE" \
        python3 "$(_memory_py)" retrieve --query "test" --top-k 3
    [[ "$status" -eq 0 ]]
}

@test "daemon clean shutdown removes socket" {
    if ! python3 -c "import fastembed" 2>/dev/null; then
        skip "fastembed not installed"
    fi

    _start_test_daemon
    [ -S "$CLAUDIO_PATH/memory.sock" ]

    # Send SIGTERM and wait for process to exit
    kill "$_daemon_pid"
    for _ in $(seq 1 20); do
        kill -0 "$_daemon_pid" 2>/dev/null || break
        sleep 0.2
    done
    _daemon_pid=""

    # Socket should be gone
    [ ! -S "$CLAUDIO_PATH/memory.sock" ]
}

@test "memory_init skips when daemon socket exists" {
    # Create a fake socket and keep it alive for the test
    python3 -c "
import socket, os, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
path = '$CLAUDIO_PATH/memory.sock'
if os.path.exists(path): os.unlink(path)
s.bind(path)
s.listen(1)
time.sleep(5)
s.close()
" &
    local sock_pid=$!
    sleep 0.2

    # memory_init should return immediately without calling python3
    MEMORY_ENABLED=1
    run memory_init
    [[ "$status" -eq 0 ]]

    kill "$sock_pid" 2>/dev/null || true
    wait "$sock_pid" 2>/dev/null || true
    rm -f "$CLAUDIO_PATH/memory.sock"
}
