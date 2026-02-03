#!/usr/bin/env bats

setup() {
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export TELEGRAM_BOT_TOKEN="test_token"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"

    mkdir -p "$BATS_TEST_TMPDIR/bin"
    echo "0" > "$BATS_TEST_TMPDIR/curl_attempts"

    # Mock log function
    log() { echo "$*" >&2; }
    export -f log

    source "$BATS_TEST_DIRNAME/../lib/telegram.sh"
}

teardown() {
    rm -rf "$BATS_TEST_TMPDIR/bin"
}

create_mock_curl() {
    local http_code="$1"
    local body="$2"
    local fail_until="${3:-0}"

    cat > "$BATS_TEST_TMPDIR/bin/curl" << EOF
#!/bin/bash
ATTEMPTS_FILE="$BATS_TEST_TMPDIR/curl_attempts"
attempts=\$(cat "\$ATTEMPTS_FILE")
echo \$((attempts + 1)) > "\$ATTEMPTS_FILE"

# Check if -w flag is present
if [[ " \$* " == *" -w "* ]]; then
    if [ "\$attempts" -lt "$fail_until" ]; then
        echo '$body'
        echo "500"
    else
        echo '$body'
        echo "$http_code"
    fi
else
    echo '$body'
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"
}

@test "telegram_api returns body on success (2xx)" {
    create_mock_curl "200" '{"ok":true}'

    result=$(telegram_api "getMe")
    [[ "$result" == '{"ok":true}' ]]
}

@test "telegram_api returns body on client error (4xx except 429)" {
    create_mock_curl "400" '{"ok":false,"description":"Bad Request"}'

    result=$(telegram_api "sendMessage")
    [[ "$result" == '{"ok":false,"description":"Bad Request"}' ]]

    # Should not retry on 4xx
    attempts=$(cat "$BATS_TEST_TMPDIR/curl_attempts")
    [[ "$attempts" == "1" ]]
}

@test "telegram_api retries on 429 rate limit" {
    # Always return 429 to test retry behavior
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
ATTEMPTS_FILE="${BATS_TEST_TMPDIR}/curl_attempts"
attempts=$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo "0")
echo $((attempts + 1)) > "$ATTEMPTS_FILE"

if [[ " $* " == *" -w "* ]]; then
    if [ "$attempts" -lt 2 ]; then
        echo '{"ok":false}'
        echo "429"
    else
        echo '{"ok":true}'
        echo "200"
    fi
else
    echo '{"ok":true}'
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"
    # Re-export so the function picks up the new PATH
    export BATS_TEST_TMPDIR

    result=$(BATS_TEST_TMPDIR="$BATS_TEST_TMPDIR" telegram_api "sendMessage")
    [[ "$result" == '{"ok":true}' ]]

    attempts=$(cat "$BATS_TEST_TMPDIR/curl_attempts")
    [[ "$attempts" == "3" ]]
}

@test "telegram_api retries on 5xx server error" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
ATTEMPTS_FILE="${BATS_TEST_TMPDIR}/curl_attempts"
attempts=$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo "0")
echo $((attempts + 1)) > "$ATTEMPTS_FILE"

if [[ " $* " == *" -w "* ]]; then
    if [ "$attempts" -lt 1 ]; then
        echo '{"ok":false}'
        echo "500"
    else
        echo '{"ok":true}'
        echo "200"
    fi
else
    echo '{"ok":true}'
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"
    export BATS_TEST_TMPDIR

    result=$(BATS_TEST_TMPDIR="$BATS_TEST_TMPDIR" telegram_api "sendMessage")
    [[ "$result" == '{"ok":true}' ]]

    attempts=$(cat "$BATS_TEST_TMPDIR/curl_attempts")
    [[ "$attempts" == "2" ]]
}

@test "telegram_api gives up after max retries" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
if [[ " $* " == *" -w "* ]]; then
    echo '{"ok":false}'
    echo "500"
else
    echo '{"ok":false}'
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    run telegram_api "sendMessage"
    [[ "$status" == "1" ]]
    # Output includes the body (last line of stdout)
    [[ "$output" == *'{"ok":false}'* ]]
}

@test "telegram_api does not retry on 403 forbidden" {
    create_mock_curl "403" '{"ok":false,"description":"Forbidden"}'

    result=$(telegram_api "sendMessage")
    [[ "$result" == '{"ok":false,"description":"Forbidden"}' ]]

    attempts=$(cat "$BATS_TEST_TMPDIR/curl_attempts")
    [[ "$attempts" == "1" ]]
}

@test "telegram_api does not retry on 404 not found" {
    create_mock_curl "404" '{"ok":false,"description":"Not Found"}'

    result=$(telegram_api "sendMessage")
    [[ "$result" == '{"ok":false,"description":"Not Found"}' ]]

    attempts=$(cat "$BATS_TEST_TMPDIR/curl_attempts")
    [[ "$attempts" == "1" ]]
}

@test "telegram_send_message includes reply_to_message_id when provided" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
# Capture all arguments to a file for inspection
echo "$@" >> "${BATS_TEST_TMPDIR}/curl_args"
if [[ " $* " == *" -w "* ]]; then
    echo '{"ok":true}'
    echo "200"
else
    echo '{"ok":true}'
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    telegram_send_message "12345" "Hello" "999"

    curl_args=$(cat "$BATS_TEST_TMPDIR/curl_args")
    [[ "$curl_args" == *"reply_to_message_id=999"* ]]
}

@test "telegram_send_message works without reply_to_message_id" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
echo "$@" >> "${BATS_TEST_TMPDIR}/curl_args"
if [[ " $* " == *" -w "* ]]; then
    echo '{"ok":true}'
    echo "200"
else
    echo '{"ok":true}'
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    telegram_send_message "12345" "Hello"

    curl_args=$(cat "$BATS_TEST_TMPDIR/curl_args")
    [[ "$curl_args" != *"reply_to_message_id"* ]]
}

@test "telegram_parse_webhook extracts message_id" {
    body='{"message":{"message_id":42,"chat":{"id":123},"text":"hello","from":{"id":456}}}'

    telegram_parse_webhook "$body"

    [[ "$WEBHOOK_MESSAGE_ID" == "42" ]]
    [[ "$WEBHOOK_CHAT_ID" == "123" ]]
    [[ "$WEBHOOK_TEXT" == "hello" ]]
}
