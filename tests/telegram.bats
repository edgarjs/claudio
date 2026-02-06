#!/usr/bin/env bats

setup() {
    export CLAUDIO_PATH="$BATS_TEST_TMPDIR"
    export CLAUDIO_LOG_FILE="$BATS_TEST_TMPDIR/claudio.log"
    export TELEGRAM_BOT_TOKEN="test_token"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"

    mkdir -p "$BATS_TEST_TMPDIR/bin"
    echo "0" > "$BATS_TEST_TMPDIR/curl_attempts"

    # Create mock curl upfront to prevent any real API calls
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'EOF'
#!/bin/bash
echo '{"ok":true}'
echo "200"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

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

@test "telegram_parse_webhook extracts photo file_id (highest resolution)" {
    body='{"message":{"message_id":42,"chat":{"id":123},"from":{"id":456},"photo":[{"file_id":"small_id","width":90,"height":90},{"file_id":"medium_id","width":320,"height":320},{"file_id":"large_id","width":800,"height":800}],"caption":"test caption"}}'

    telegram_parse_webhook "$body"

    [[ "$WEBHOOK_CHAT_ID" == "123" ]]
    [[ "$WEBHOOK_PHOTO_FILE_ID" == "large_id" ]]
    [[ "$WEBHOOK_CAPTION" == "test caption" ]]
    [[ -z "$WEBHOOK_TEXT" ]]
}

@test "telegram_parse_webhook extracts document fields" {
    body='{"message":{"message_id":42,"chat":{"id":123},"from":{"id":456},"document":{"file_id":"doc_id","mime_type":"image/png","file_name":"screenshot.png"}}}'

    telegram_parse_webhook "$body"

    [[ "$WEBHOOK_DOC_FILE_ID" == "doc_id" ]]
    [[ "$WEBHOOK_DOC_MIME" == "image/png" ]]
    [[ "$WEBHOOK_DOC_FILE_NAME" == "screenshot.png" ]]
}

@test "telegram_parse_webhook extracts non-image document fields" {
    body='{"message":{"message_id":42,"chat":{"id":123},"from":{"id":456},"document":{"file_id":"pdf_id","mime_type":"application/pdf","file_name":"report.pdf"},"caption":"check this"}}'

    telegram_parse_webhook "$body"

    [[ "$WEBHOOK_DOC_FILE_ID" == "pdf_id" ]]
    [[ "$WEBHOOK_DOC_MIME" == "application/pdf" ]]
    [[ "$WEBHOOK_DOC_FILE_NAME" == "report.pdf" ]]
    [[ "$WEBHOOK_CAPTION" == "check this" ]]
}

@test "telegram_parse_webhook extracts caption without photo" {
    body='{"message":{"message_id":42,"chat":{"id":123},"from":{"id":456},"caption":"just a caption"}}'

    telegram_parse_webhook "$body"

    [[ "$WEBHOOK_CAPTION" == "just a caption" ]]
    [[ -z "$WEBHOOK_TEXT" ]]
    [[ -z "$WEBHOOK_PHOTO_FILE_ID" ]]
}

@test "telegram_get_image_info detects compressed photo" {
    WEBHOOK_PHOTO_FILE_ID="photo_id"
    WEBHOOK_DOC_FILE_ID=""
    WEBHOOK_DOC_MIME=""

    telegram_get_image_info

    [[ "$WEBHOOK_IMAGE_FILE_ID" == "photo_id" ]]
    [[ "$WEBHOOK_IMAGE_EXT" == "jpg" ]]
}

@test "telegram_get_image_info detects document image" {
    WEBHOOK_PHOTO_FILE_ID=""
    WEBHOOK_DOC_FILE_ID="doc_id"
    WEBHOOK_DOC_MIME="image/png"

    telegram_get_image_info

    [[ "$WEBHOOK_IMAGE_FILE_ID" == "doc_id" ]]
    [[ "$WEBHOOK_IMAGE_EXT" == "png" ]]
}

@test "telegram_get_image_info ignores non-image document" {
    WEBHOOK_PHOTO_FILE_ID=""
    WEBHOOK_DOC_FILE_ID="doc_id"
    WEBHOOK_DOC_MIME="application/pdf"

    run telegram_get_image_info
    [[ "$status" -ne 0 ]]
}

@test "telegram_get_image_info prefers photo over document" {
    WEBHOOK_PHOTO_FILE_ID="photo_id"
    WEBHOOK_DOC_FILE_ID="doc_id"
    WEBHOOK_DOC_MIME="image/png"

    telegram_get_image_info

    [[ "$WEBHOOK_IMAGE_FILE_ID" == "photo_id" ]]
    [[ "$WEBHOOK_IMAGE_EXT" == "jpg" ]]
}

@test "telegram_download_file resolves file_id and downloads binary" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"getFile"* ]]; then
    # getFile API call (has -w flag from telegram_api)
    if [[ " $* " == *" -w "* ]]; then
        echo '{"ok":true,"result":{"file_path":"photos/file_123.jpg"}}'
        echo "200"
    else
        echo '{"ok":true,"result":{"file_path":"photos/file_123.jpg"}}'
    fi
elif [[ "$*" == *"/file/bot"* ]]; then
    # Direct file download — write valid JPEG magic bytes
    output_file=$(echo "$@" | grep -oE '\-o [^ ]+' | cut -d' ' -f2)
    printf '\xff\xd8\xff\xe0fake_jpeg_data' > "$output_file"
else
    echo '{"ok":true}'
    echo "200"
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.jpg"
    telegram_download_file "test_file_id" "$output_file"
    [[ -f "$output_file" ]]
}

@test "telegram_download_file rejects path traversal in file_path" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ " $* " == *" -w "* ]]; then
    echo '{"ok":true,"result":{"file_path":"../../../etc/passwd"}}'
    echo "200"
else
    echo '{"ok":true,"result":{"file_path":"../../../etc/passwd"}}'
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.jpg"
    run telegram_download_file "test_file_id" "$output_file"
    [[ "$status" -ne 0 ]]
    [[ ! -f "$output_file" ]]
}

@test "telegram_download_file rejects non-image file" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"getFile"* ]]; then
    if [[ " $* " == *" -w "* ]]; then
        echo '{"ok":true,"result":{"file_path":"photos/file_123.jpg"}}'
        echo "200"
    else
        echo '{"ok":true,"result":{"file_path":"photos/file_123.jpg"}}'
    fi
elif [[ "$*" == *"/file/bot"* ]]; then
    # Write non-image content (plain text)
    output_file=$(echo "$@" | grep -oE '\-o [^ ]+' | cut -d' ' -f2)
    echo "this is not an image" > "$output_file"
else
    echo '{"ok":true}'
    echo "200"
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.jpg"
    run telegram_download_file "test_file_id" "$output_file"
    [[ "$status" -ne 0 ]]
    # File should be cleaned up on validation failure
    [[ ! -f "$output_file" ]]
}

@test "telegram_download_file rejects file_path with special characters" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ " $* " == *" -w "* ]]; then
    echo '{"ok":true,"result":{"file_path":"photos/file%20name.jpg"}}'
    echo "200"
else
    echo '{"ok":true,"result":{"file_path":"photos/file%20name.jpg"}}'
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.jpg"
    run telegram_download_file "test_file_id" "$output_file"
    [[ "$status" -ne 0 ]]
}

@test "telegram_download_file rejects RIFF non-WebP file" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"getFile"* ]]; then
    if [[ " $* " == *" -w "* ]]; then
        echo '{"ok":true,"result":{"file_path":"photos/file_123.webp"}}'
        echo "200"
    else
        echo '{"ok":true,"result":{"file_path":"photos/file_123.webp"}}'
    fi
elif [[ "$*" == *"/file/bot"* ]]; then
    # Write RIFF header with AVI subtype (not WebP)
    output_file=$(echo "$@" | grep -oE '\-o [^ ]+' | cut -d' ' -f2)
    printf 'RIFF\x00\x00\x00\x00AVI fake_data' > "$output_file"
else
    echo '{"ok":true}'
    echo "200"
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.webp"
    run telegram_download_file "test_file_id" "$output_file"
    [[ "$status" -ne 0 ]]
}

@test "telegram_download_file accepts valid WebP file" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"getFile"* ]]; then
    if [[ " $* " == *" -w "* ]]; then
        echo '{"ok":true,"result":{"file_path":"photos/file_123.webp"}}'
        echo "200"
    else
        echo '{"ok":true,"result":{"file_path":"photos/file_123.webp"}}'
    fi
elif [[ "$*" == *"/file/bot"* ]]; then
    # Write valid WebP header: RIFF + 4 size bytes + WEBP
    output_file=$(echo "$@" | grep -oE '\-o [^ ]+' | cut -d' ' -f2)
    printf 'RIFF\x00\x10\x00\x00WEBP_fake_data' > "$output_file"
else
    echo '{"ok":true}'
    echo "200"
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.webp"
    telegram_download_file "test_file_id" "$output_file"
    [[ -f "$output_file" ]]
}

# --- telegram_download_document tests ---

@test "telegram_download_document resolves file_id and downloads document" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"getFile"* ]]; then
    if [[ " $* " == *" -w "* ]]; then
        echo '{"ok":true,"result":{"file_path":"documents/file_456.pdf"}}'
        echo "200"
    else
        echo '{"ok":true,"result":{"file_path":"documents/file_456.pdf"}}'
    fi
elif [[ "$*" == *"/file/bot"* ]]; then
    output_file=$(echo "$@" | grep -oE '\-o [^ ]+' | cut -d' ' -f2)
    printf '%%PDF-1.4 fake pdf content' > "$output_file"
else
    echo '{"ok":true}'
    echo "200"
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.pdf"
    telegram_download_document "test_file_id" "$output_file"
    [[ -f "$output_file" ]]
}

@test "telegram_download_document rejects path traversal" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ " $* " == *" -w "* ]]; then
    echo '{"ok":true,"result":{"file_path":"../../../etc/passwd"}}'
    echo "200"
else
    echo '{"ok":true,"result":{"file_path":"../../../etc/passwd"}}'
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.txt"
    run telegram_download_document "test_file_id" "$output_file"
    [[ "$status" -ne 0 ]]
    [[ ! -f "$output_file" ]]
}

@test "telegram_download_document rejects empty file" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"getFile"* ]]; then
    if [[ " $* " == *" -w "* ]]; then
        echo '{"ok":true,"result":{"file_path":"documents/empty.txt"}}'
        echo "200"
    else
        echo '{"ok":true,"result":{"file_path":"documents/empty.txt"}}'
    fi
elif [[ "$*" == *"/file/bot"* ]]; then
    output_file=$(echo "$@" | grep -oE '\-o [^ ]+' | cut -d' ' -f2)
    : > "$output_file"
else
    echo '{"ok":true}'
    echo "200"
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.txt"
    run telegram_download_document "test_file_id" "$output_file"
    [[ "$status" -ne 0 ]]
    [[ ! -f "$output_file" ]]
}

@test "telegram_download_document accepts any file type (no magic byte check)" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"getFile"* ]]; then
    if [[ " $* " == *" -w "* ]]; then
        echo '{"ok":true,"result":{"file_path":"documents/data.csv"}}'
        echo "200"
    else
        echo '{"ok":true,"result":{"file_path":"documents/data.csv"}}'
    fi
elif [[ "$*" == *"/file/bot"* ]]; then
    output_file=$(echo "$@" | grep -oE '\-o [^ ]+' | cut -d' ' -f2)
    echo "name,age,city" > "$output_file"
else
    echo '{"ok":true}'
    echo "200"
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.csv"
    telegram_download_document "test_file_id" "$output_file"
    [[ -f "$output_file" ]]
}

@test "telegram_download_document rejects file_path with special characters" {
    cat > "$BATS_TEST_TMPDIR/bin/curl" << 'MOCK'
#!/bin/bash
if [[ " $* " == *" -w "* ]]; then
    echo '{"ok":true,"result":{"file_path":"documents/file name.pdf"}}'
    echo "200"
else
    echo '{"ok":true,"result":{"file_path":"documents/file name.pdf"}}'
fi
MOCK
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    local output_file="$BATS_TEST_TMPDIR/downloaded.pdf"
    run telegram_download_document "test_file_id" "$output_file"
    [[ "$status" -ne 0 ]]
}
