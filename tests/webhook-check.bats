#!/usr/bin/env bats

setup() {
    export BATS_TEST_TMPDIR="${BATS_TEST_TMPDIR:-/tmp/bats-$$}"
    mkdir -p "$BATS_TEST_TMPDIR"
    export HOME="$BATS_TEST_TMPDIR"
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR/.claudio"
    mkdir -p "$CLAUDIO_PATH"

    # Create mock curl in path
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    mkdir -p "$BATS_TEST_TMPDIR/bin"
}

teardown() {
    rm -rf "$BATS_TEST_TMPDIR"
}

create_env_file() {
    cat > "$CLAUDIO_PATH/service.env" << EOF
TELEGRAM_BOT_TOKEN=test-token-123
WEBHOOK_URL=https://test.example.com
WEBHOOK_SECRET=secret123
EOF
}

create_mock_curl() {
    local webhook_url="$1"
    cat > "$BATS_TEST_TMPDIR/bin/curl" << EOF
#!/bin/bash
if [[ "\$*" == *"getWebhookInfo"* ]]; then
    echo '{"ok":true,"result":{"url":"${webhook_url}"}}'
elif [[ "\$*" == *"setWebhook"* ]]; then
    echo '{"ok":true,"result":true}'
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"
}

@test "webhook-check exits 0 when webhook is correctly configured" {
    create_env_file
    create_mock_curl "https://test.example.com/telegram/webhook"

    run "$BATS_TEST_DIRNAME/../lib/webhook-check.sh"

    [ "$status" -eq 0 ]
    # No log file should be created when everything is fine
    [ ! -f "$CLAUDIO_PATH/webhook-check.log" ]
}

@test "webhook-check re-registers when webhook URL is empty" {
    create_env_file

    # First call returns empty URL, second call (setWebhook) succeeds
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
if [[ "$*" == *"getWebhookInfo"* ]]; then
    echo '{"ok":true,"result":{"url":""}}'
elif [[ "$*" == *"setWebhook"* ]]; then
    echo '{"ok":true,"result":true}'
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    run "$BATS_TEST_DIRNAME/../lib/webhook-check.sh"

    [ "$status" -eq 0 ]
    [ -f "$CLAUDIO_PATH/webhook-check.log" ]
    grep -q "Webhook mismatch detected" "$CLAUDIO_PATH/webhook-check.log"
    grep -q "Webhook re-registered successfully" "$CLAUDIO_PATH/webhook-check.log"
}

@test "webhook-check re-registers when webhook URL is wrong" {
    create_env_file
    create_mock_curl "https://wrong-url.example.com/webhook"

    # Override to handle setWebhook too
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
if [[ "$*" == *"getWebhookInfo"* ]]; then
    echo '{"ok":true,"result":{"url":"https://wrong-url.example.com/webhook"}}'
elif [[ "$*" == *"setWebhook"* ]]; then
    echo '{"ok":true,"result":true}'
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    run "$BATS_TEST_DIRNAME/../lib/webhook-check.sh"

    [ "$status" -eq 0 ]
    grep -q "Webhook mismatch detected" "$CLAUDIO_PATH/webhook-check.log"
}

@test "webhook-check fails when env file is missing" {
    # Don't create env file

    run "$BATS_TEST_DIRNAME/../lib/webhook-check.sh"

    [ "$status" -eq 1 ]
}

@test "webhook-check fails when TELEGRAM_BOT_TOKEN is not set" {
    cat > "$CLAUDIO_PATH/service.env" << 'EOF'
WEBHOOK_URL=https://test.example.com
EOF

    run "$BATS_TEST_DIRNAME/../lib/webhook-check.sh"

    [ "$status" -eq 1 ]
    grep -q "TELEGRAM_BOT_TOKEN not set" "$CLAUDIO_PATH/webhook-check.log"
}

@test "webhook-check fails when WEBHOOK_URL is not set" {
    cat > "$CLAUDIO_PATH/service.env" << 'EOF'
TELEGRAM_BOT_TOKEN=test-token
EOF

    run "$BATS_TEST_DIRNAME/../lib/webhook-check.sh"

    [ "$status" -eq 1 ]
    grep -q "WEBHOOK_URL not set" "$CLAUDIO_PATH/webhook-check.log"
}

@test "cron_install adds cron entry" {
    source "$BATS_TEST_DIRNAME/../lib/service.sh"

    # Use a fake crontab command
    cat > "$BATS_TEST_TMPDIR/bin/crontab" << 'EOF'
#!/bin/bash
if [ "$1" = "-l" ]; then
    cat "$HOME/.fake_crontab" 2>/dev/null || true
else
    cat > "$HOME/.fake_crontab"
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/crontab"

    run cron_install

    [ "$status" -eq 0 ]
    grep -q "webhook-check.sh" "$HOME/.fake_crontab"
    grep -q "claudio-webhook-check" "$HOME/.fake_crontab"
}

@test "cron_uninstall removes cron entry" {
    source "$BATS_TEST_DIRNAME/../lib/service.sh"

    # Set up fake crontab with existing entry
    cat > "$BATS_TEST_TMPDIR/bin/crontab" << 'EOF'
#!/bin/bash
if [ "$1" = "-l" ]; then
    cat "$HOME/.fake_crontab" 2>/dev/null || true
else
    cat > "$HOME/.fake_crontab"
fi
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/crontab"

    echo "*/5 * * * * /path/to/webhook-check.sh # claudio-webhook-check" > "$HOME/.fake_crontab"

    run cron_uninstall

    [ "$status" -eq 0 ]
    ! grep -q "claudio-webhook-check" "$HOME/.fake_crontab"
}
