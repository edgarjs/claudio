#!/usr/bin/env bats

# Tests for multi-bot config: migration, loading, saving, listing

setup() {
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_ENV_FILE="$CLAUDIO_PATH/service.env"
    export CLAUDIO_LOG_FILE="$CLAUDIO_PATH/claudio.log"
    export CLAUDIO_DB_FILE=""
    export CLAUDIO_BOT_ID=""
    export CLAUDIO_BOT_DIR=""
    # Reset per-bot vars to prevent pollution between tests
    unset TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID WEBHOOK_SECRET MODEL MAX_HISTORY_LINES
    mkdir -p "$CLAUDIO_PATH"

    # Source config module
    source "$BATS_TEST_DIRNAME/../lib/config.sh"
}

teardown() {
    rm -rf "$BATS_TEST_TMPDIR"
}

# ── Migration tests ──────────────────────────────────────────────

@test "migration creates bots/claudio/ from single-bot service.env" {
    # Write a legacy single-bot service.env
    cat > "$CLAUDIO_ENV_FILE" << 'EOF'
PORT="8421"
MODEL="sonnet"
TELEGRAM_BOT_TOKEN="123:ABC"
TELEGRAM_CHAT_ID="456"
WEBHOOK_URL="https://example.com"
TUNNEL_NAME="claudio"
TUNNEL_HOSTNAME="claudio.example.com"
WEBHOOK_SECRET="secret123"
WEBHOOK_RETRY_DELAY="60"
ELEVENLABS_API_KEY=""
ELEVENLABS_VOICE_ID="voice_id"
ELEVENLABS_MODEL="eleven_multilingual_v2"
ELEVENLABS_STT_MODEL="scribe_v1"
MEMORY_ENABLED="1"
MEMORY_EMBEDDING_MODEL="sentence-transformers/all-MiniLM-L6-v2"
MEMORY_CONSOLIDATION_MODEL="haiku"
MAX_HISTORY_LINES="100"
EOF

    # Run init (triggers migration)
    claudio_init

    # Verify bot dir was created
    [ -d "$CLAUDIO_PATH/bots/claudio" ]
    [ -f "$CLAUDIO_PATH/bots/claudio/bot.env" ]

    # Verify per-bot vars are in bot.env
    run grep 'TELEGRAM_BOT_TOKEN' "$CLAUDIO_PATH/bots/claudio/bot.env"
    [[ "$output" == *"123:ABC"* ]]

    run grep 'TELEGRAM_CHAT_ID' "$CLAUDIO_PATH/bots/claudio/bot.env"
    [[ "$output" == *"456"* ]]

    run grep 'WEBHOOK_SECRET' "$CLAUDIO_PATH/bots/claudio/bot.env"
    [[ "$output" == *"secret123"* ]]

    run grep 'MODEL' "$CLAUDIO_PATH/bots/claudio/bot.env"
    [[ "$output" == *"sonnet"* ]]
}

@test "migration moves history.db to per-bot dir" {
    cat > "$CLAUDIO_ENV_FILE" << 'EOF'
TELEGRAM_BOT_TOKEN="123:ABC"
TELEGRAM_CHAT_ID="456"
WEBHOOK_SECRET="secret123"
MODEL="haiku"
MAX_HISTORY_LINES="100"
EOF
    # Create a fake history.db
    echo "test data" > "$CLAUDIO_PATH/history.db"

    claudio_init

    # DB should be moved
    [ ! -f "$CLAUDIO_PATH/history.db" ]
    [ -f "$CLAUDIO_PATH/bots/claudio/history.db" ]
    [[ "$(cat "$CLAUDIO_PATH/bots/claudio/history.db")" == "test data" ]]
}

@test "migration removes per-bot vars from service.env" {
    cat > "$CLAUDIO_ENV_FILE" << 'EOF'
PORT="8421"
TELEGRAM_BOT_TOKEN="123:ABC"
TELEGRAM_CHAT_ID="456"
WEBHOOK_SECRET="secret123"
MODEL="haiku"
WEBHOOK_URL="https://example.com"
TUNNEL_NAME="claudio"
TUNNEL_HOSTNAME="claudio.example.com"
MAX_HISTORY_LINES="100"
EOF

    claudio_init

    # service.env should NOT contain per-bot vars
    run grep 'TELEGRAM_BOT_TOKEN' "$CLAUDIO_ENV_FILE"
    [ "$status" -ne 0 ]
    run grep 'TELEGRAM_CHAT_ID' "$CLAUDIO_ENV_FILE"
    [ "$status" -ne 0 ]
    run grep 'WEBHOOK_SECRET' "$CLAUDIO_ENV_FILE"
    [ "$status" -ne 0 ]

    # service.env SHOULD still contain global vars
    run grep 'PORT' "$CLAUDIO_ENV_FILE"
    [ "$status" -eq 0 ]
    run grep 'WEBHOOK_URL' "$CLAUDIO_ENV_FILE"
    [ "$status" -eq 0 ]
}

@test "migration preserves unmanaged vars in service.env" {
    cat > "$CLAUDIO_ENV_FILE" << 'EOF'
PORT="8421"
TELEGRAM_BOT_TOKEN="123:ABC"
TELEGRAM_CHAT_ID="456"
WEBHOOK_SECRET="secret123"
MODEL="haiku"
WEBHOOK_URL="https://example.com"
TUNNEL_NAME="claudio"
TUNNEL_HOSTNAME="claudio.example.com"
MAX_HISTORY_LINES="100"
HASS_TOKEN="my-ha-token"
ALEXA_SKILL_ID="amzn1.ask.skill.xyz"
EOF

    claudio_init

    # Unmanaged vars should be preserved
    run grep 'HASS_TOKEN' "$CLAUDIO_ENV_FILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"my-ha-token"* ]]

    run grep 'ALEXA_SKILL_ID' "$CLAUDIO_ENV_FILE"
    [ "$status" -eq 0 ]
}

@test "migration is idempotent — skips if bots/ exists" {
    cat > "$CLAUDIO_ENV_FILE" << 'EOF'
PORT="8421"
TELEGRAM_BOT_TOKEN="123:ABC"
TELEGRAM_CHAT_ID="456"
WEBHOOK_SECRET="secret123"
MODEL="haiku"
MAX_HISTORY_LINES="100"
EOF

    # First migration
    claudio_init

    # Modify bot.env to detect if it gets overwritten
    echo "# marker" >> "$CLAUDIO_PATH/bots/claudio/bot.env"

    # Second call should not re-migrate
    claudio_init

    run grep 'marker' "$CLAUDIO_PATH/bots/claudio/bot.env"
    [ "$status" -eq 0 ]
}

@test "migration skips when no TELEGRAM_BOT_TOKEN (fresh install)" {
    cat > "$CLAUDIO_ENV_FILE" << 'EOF'
PORT="8421"
EOF

    claudio_init

    [ ! -d "$CLAUDIO_PATH/bots" ]
}

# ── claudio_load_bot tests ───────────────────────────────────────

@test "claudio_load_bot sets per-bot globals" {
    mkdir -p "$CLAUDIO_PATH/bots/testbot"
    cat > "$CLAUDIO_PATH/bots/testbot/bot.env" << 'EOF'
TELEGRAM_BOT_TOKEN="test_token"
TELEGRAM_CHAT_ID="test_chat"
WEBHOOK_SECRET="test_secret"
MODEL="opus"
MAX_HISTORY_LINES="50"
EOF

    claudio_load_bot "testbot"

    [ "$CLAUDIO_BOT_ID" = "testbot" ]
    [ "$CLAUDIO_BOT_DIR" = "$CLAUDIO_PATH/bots/testbot" ]
    [ "$CLAUDIO_DB_FILE" = "$CLAUDIO_PATH/bots/testbot/history.db" ]
    [ "$TELEGRAM_BOT_TOKEN" = "test_token" ]
    [ "$TELEGRAM_CHAT_ID" = "test_chat" ]
    [ "$WEBHOOK_SECRET" = "test_secret" ]
    [ "$MODEL" = "opus" ]
}

@test "claudio_load_bot fails for missing bot" {
    run claudio_load_bot "nonexistent"
    [ "$status" -ne 0 ]
    [[ "$output" == *"not found"* ]]
}

# ── claudio_save_bot_env tests ───────────────────────────────────

@test "claudio_save_bot_env writes per-bot vars" {
    mkdir -p "$CLAUDIO_PATH/bots/testbot"
    export CLAUDIO_BOT_DIR="$CLAUDIO_PATH/bots/testbot"
    TELEGRAM_BOT_TOKEN="save_token"
    TELEGRAM_CHAT_ID="save_chat"
    WEBHOOK_SECRET="save_secret"
    MODEL="sonnet"
    MAX_HISTORY_LINES="75"

    claudio_save_bot_env

    [ -f "$CLAUDIO_BOT_DIR/bot.env" ]

    run grep 'TELEGRAM_BOT_TOKEN' "$CLAUDIO_BOT_DIR/bot.env"
    [[ "$output" == *"save_token"* ]]
    run grep 'MODEL' "$CLAUDIO_BOT_DIR/bot.env"
    [[ "$output" == *"sonnet"* ]]
}

@test "claudio_save_bot_env fails without CLAUDIO_BOT_DIR" {
    unset CLAUDIO_BOT_DIR
    export CLAUDIO_BOT_DIR=""

    run claudio_save_bot_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"CLAUDIO_BOT_DIR not set"* ]]
}

# ── claudio_list_bots tests ──────────────────────────────────────

@test "claudio_list_bots lists configured bots" {
    mkdir -p "$CLAUDIO_PATH/bots/alpha"
    mkdir -p "$CLAUDIO_PATH/bots/beta"
    touch "$CLAUDIO_PATH/bots/alpha/bot.env"
    touch "$CLAUDIO_PATH/bots/beta/bot.env"

    result=$(claudio_list_bots)

    [[ "$result" == *"alpha"* ]]
    [[ "$result" == *"beta"* ]]
}

@test "claudio_list_bots returns empty when no bots dir" {
    result=$(claudio_list_bots)
    [ -z "$result" ]
}

@test "claudio_list_bots skips dirs without bot.env" {
    mkdir -p "$CLAUDIO_PATH/bots/valid"
    mkdir -p "$CLAUDIO_PATH/bots/invalid"
    touch "$CLAUDIO_PATH/bots/valid/bot.env"
    # invalid/ has no bot.env

    result=$(claudio_list_bots)
    [[ "$result" == *"valid"* ]]
    [[ "$result" != *"invalid"* ]]
}

# ── claudio_save_env global-only tests ───────────────────────────

@test "claudio_save_env writes only global vars" {
    PORT="8421"
    WEBHOOK_URL="https://example.com"
    TUNNEL_NAME="claudio"
    TUNNEL_HOSTNAME="claudio.example.com"
    touch "$CLAUDIO_ENV_FILE"

    claudio_save_env

    # Should contain global vars
    run grep 'PORT' "$CLAUDIO_ENV_FILE"
    [ "$status" -eq 0 ]
    run grep 'WEBHOOK_URL' "$CLAUDIO_ENV_FILE"
    [ "$status" -eq 0 ]

    # Should NOT contain per-bot vars
    run grep 'TELEGRAM_BOT_TOKEN' "$CLAUDIO_ENV_FILE"
    [ "$status" -ne 0 ]
    run grep 'TELEGRAM_CHAT_ID' "$CLAUDIO_ENV_FILE"
    [ "$status" -ne 0 ]
    run grep 'WEBHOOK_SECRET' "$CLAUDIO_ENV_FILE"
    [ "$status" -ne 0 ]
    run grep '^MODEL=' "$CLAUDIO_ENV_FILE"
    [ "$status" -ne 0 ]
    run grep '^MAX_HISTORY_LINES=' "$CLAUDIO_ENV_FILE"
    [ "$status" -ne 0 ]
}

# ── server.py parse_env_file tests ───────────────────────────────

@test "server.py parse_env_file parses quoted values" {
    cat > "$BATS_TEST_TMPDIR/test.env" << 'EOF'
TELEGRAM_BOT_TOKEN="123:ABC"
TELEGRAM_CHAT_ID="456"
EOF

    run python3 -c "
import sys; sys.path.insert(0, '$BATS_TEST_DIRNAME/../lib')
from server import parse_env_file
cfg = parse_env_file('$BATS_TEST_TMPDIR/test.env')
print(cfg.get('TELEGRAM_BOT_TOKEN', ''))
print(cfg.get('TELEGRAM_CHAT_ID', ''))
"
    [ "$status" -eq 0 ]
    [[ "${lines[0]}" == "123:ABC" ]]
    [[ "${lines[1]}" == "456" ]]
}

@test "server.py parse_env_file handles escaped values" {
    cat > "$BATS_TEST_TMPDIR/test.env" << 'EOF'
VALUE="has \"quotes\" and \\backslash"
EOF

    run python3 -c "
import sys; sys.path.insert(0, '$BATS_TEST_DIRNAME/../lib')
from server import parse_env_file
cfg = parse_env_file('$BATS_TEST_TMPDIR/test.env')
print(cfg.get('VALUE', ''))
"
    [ "$status" -eq 0 ]
    [[ "$output" == 'has "quotes" and \backslash' ]]
}

@test "server.py load_bots loads from bots directory" {
    mkdir -p "$CLAUDIO_PATH/bots/bot1"
    cat > "$CLAUDIO_PATH/bots/bot1/bot.env" << 'EOF'
TELEGRAM_BOT_TOKEN="token1"
TELEGRAM_CHAT_ID="chat1"
WEBHOOK_SECRET="secret1"
MODEL="haiku"
EOF

    run python3 -c "
import sys, os
sys.stderr = open(os.devnull, 'w')
sys.path.insert(0, '$BATS_TEST_DIRNAME/../lib')
os.environ['HOME'] = '$BATS_TEST_TMPDIR'
import server
server.CLAUDIO_PATH = '$CLAUDIO_PATH'
server.load_bots()
print(len(server.bots))
print(server.bots.get('bot1', {}).get('token', ''))
print(server.bots.get('bot1', {}).get('chat_id', ''))
print(server.bots.get('bot1', {}).get('secret', ''))
"
    [ "$status" -eq 0 ]
    [[ "${lines[0]}" == "1" ]]
    [[ "${lines[1]}" == "token1" ]]
    [[ "${lines[2]}" == "chat1" ]]
    [[ "${lines[3]}" == "secret1" ]]
}

@test "server.py match_bot_by_secret dispatches correctly" {
    mkdir -p "$CLAUDIO_PATH/bots/bot_a"
    mkdir -p "$CLAUDIO_PATH/bots/bot_b"
    cat > "$CLAUDIO_PATH/bots/bot_a/bot.env" << 'EOF'
TELEGRAM_BOT_TOKEN="token_a"
TELEGRAM_CHAT_ID="chat_a"
WEBHOOK_SECRET="secret_aaa"
EOF
    cat > "$CLAUDIO_PATH/bots/bot_b/bot.env" << 'EOF'
TELEGRAM_BOT_TOKEN="token_b"
TELEGRAM_CHAT_ID="chat_b"
WEBHOOK_SECRET="secret_bbb"
EOF

    run python3 -c "
import sys, os
sys.stderr = open(os.devnull, 'w')
sys.path.insert(0, '$BATS_TEST_DIRNAME/../lib')
os.environ['HOME'] = '$BATS_TEST_TMPDIR'
import server
server.CLAUDIO_PATH = '$CLAUDIO_PATH'
server.load_bots()

# Match bot_a
bot_id, cfg = server.match_bot_by_secret('secret_aaa')
print(f'{bot_id}:{cfg[\"token\"]}')

# Match bot_b
bot_id, cfg = server.match_bot_by_secret('secret_bbb')
print(f'{bot_id}:{cfg[\"token\"]}')

# No match
bot_id, cfg = server.match_bot_by_secret('wrong_secret')
print(f'{bot_id}:{cfg}')
"
    [ "$status" -eq 0 ]
    [[ "${lines[0]}" == "bot_a:token_a" ]]
    [[ "${lines[1]}" == "bot_b:token_b" ]]
    [[ "${lines[2]}" == "None:None" ]]
}

@test "server.py skips bots without token" {
    mkdir -p "$CLAUDIO_PATH/bots/incomplete"
    cat > "$CLAUDIO_PATH/bots/incomplete/bot.env" << 'EOF'
TELEGRAM_CHAT_ID="chat1"
WEBHOOK_SECRET="secret1"
EOF

    run python3 -c "
import sys, os
sys.stderr = open(os.devnull, 'w')
sys.path.insert(0, '$BATS_TEST_DIRNAME/../lib')
os.environ['HOME'] = '$BATS_TEST_TMPDIR'
import server
server.CLAUDIO_PATH = '$CLAUDIO_PATH'
server.load_bots()
print(len(server.bots))
"
    [ "$status" -eq 0 ]
    [[ "${lines[0]}" == "0" ]]
}
