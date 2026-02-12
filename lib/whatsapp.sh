#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

WHATSAPP_API="https://graph.facebook.com/v21.0"

# Helper: Create secure temporary config file for curl
# Returns path via stdout, caller must cleanup
_whatsapp_curl_config() {
    local endpoint="$1"
    local config_file
    config_file=$(mktemp "${CLAUDIO_PATH}/tmp/curl-config-XXXXXX") || return 1
    chmod 600 "$config_file"

    printf 'url = "%s/%s/%s"\n' "$WHATSAPP_API" "$WHATSAPP_PHONE_NUMBER_ID" "$endpoint" > "$config_file"
    printf 'header = "Authorization: Bearer %s"\n' "$WHATSAPP_ACCESS_TOKEN" >> "$config_file"

    echo "$config_file"
}

whatsapp_api() {
    local endpoint="$1"
    shift

    local max_retries=4
    local attempt=0
    local response http_code body
    local config_file

    # Create secure config file (prevents credential exposure in process list)
    config_file=$(_whatsapp_curl_config "$endpoint") || {
        log_error "whatsapp" "Failed to create curl config"
        return 1
    }
    trap 'rm -f "$config_file"' RETURN

    while [ $attempt -le $max_retries ]; do
        response=$(curl -s -w "\n%{http_code}" --config "$config_file" "$@")
        http_code=$(echo "$response" | tail -n1)
        body=$(echo "$response" | sed '$d')

        # Success or client error (4xx except 429) - don't retry
        if [[ "$http_code" =~ ^2 ]] || { [[ "$http_code" =~ ^4 ]] && [ "$http_code" != "429" ]; }; then
            echo "$body"
            return 0
        fi

        # Retryable: 429 (rate limit) or 5xx (server error)
        if [ $attempt -lt $max_retries ]; then
            local delay=$(( 2 ** attempt ))  # Exponential backoff
            log "whatsapp" "API error (HTTP $http_code), retrying in ${delay}s..."
            sleep "$delay"
        fi

        ((attempt++)) || true
    done

    # All retries exhausted
    log_error "whatsapp" "API failed after $((max_retries + 1)) attempts (HTTP $http_code)"
    echo "$body"
    return 1
}

whatsapp_send_message() {
    local to="$1"
    local text="$2"
    local reply_to_message_id="${3:-}"

    # WhatsApp has a 4096 char limit per message
    local max_len=4096
    local is_first=true
    while [ ${#text} -gt 0 ]; do
        local chunk="${text:0:$max_len}"
        text="${text:$max_len}"

        # Build JSON payload with jq for safe variable handling
        local payload
        if [ "$is_first" = true ] && [ -n "$reply_to_message_id" ]; then
            payload=$(jq -n \
                --arg to "$to" \
                --arg text "$chunk" \
                --arg mid "$reply_to_message_id" \
                '{
                    messaging_product: "whatsapp",
                    recipient_type: "individual",
                    to: $to,
                    type: "text",
                    text: { preview_url: false, body: $text }
                } | . + {context: {message_id: $mid}}')
        else
            payload=$(jq -n \
                --arg to "$to" \
                --arg text "$chunk" \
                '{
                    messaging_product: "whatsapp",
                    recipient_type: "individual",
                    to: $to,
                    type: "text",
                    text: { preview_url: false, body: $text }
                }')
        fi
        is_first=false

        local result
        result=$(whatsapp_api "messages" \
            -H "Content-Type: application/json" \
            -d "$payload")

        local success
        success=$(echo "$result" | jq -r '.messages[0].id // empty' 2>/dev/null)
        if [ -z "$success" ]; then
            log_error "whatsapp" "Failed to send message: $result"
        fi
    done
}

whatsapp_setup() {
    local bot_id="${1:-}"

    echo "=== Claudio WhatsApp Business API Setup ==="
    if [ -n "$bot_id" ]; then
        echo "Bot: $bot_id"
    fi
    echo ""
    echo "You'll need the following from your WhatsApp Business account:"
    echo "1. Phone Number ID (from Meta Business Suite)"
    echo "2. Access Token (permanent token from Meta for Developers)"
    echo "3. App Secret (from your Meta app settings)"
    echo "4. Authorized phone number (the number you want to receive messages from)"
    echo ""

    read -rp "Enter your WhatsApp Phone Number ID: " phone_id
    if [ -z "$phone_id" ]; then
        print_error "Phone Number ID cannot be empty."
        exit 1
    fi

    read -rp "Enter your WhatsApp Access Token: " access_token
    if [ -z "$access_token" ]; then
        print_error "Access Token cannot be empty."
        exit 1
    fi

    read -rp "Enter your WhatsApp App Secret: " app_secret
    if [ -z "$app_secret" ]; then
        print_error "App Secret cannot be empty."
        exit 1
    fi

    read -rp "Enter authorized phone number (format: 1234567890): " phone_number
    if [ -z "$phone_number" ]; then
        print_error "Phone number cannot be empty."
        exit 1
    fi

    # Generate verify token
    local verify_token
    verify_token=$(openssl rand -hex 32) || {
        print_error "Failed to generate verify token"
        exit 1
    }

    export WHATSAPP_PHONE_NUMBER_ID="$phone_id"
    export WHATSAPP_ACCESS_TOKEN="$access_token"
    export WHATSAPP_APP_SECRET="$app_secret"
    export WHATSAPP_PHONE_NUMBER="$phone_number"
    export WHATSAPP_VERIFY_TOKEN="$verify_token"

    # Verify credentials by calling the API
    local config_file test_result
    config_file=$(mktemp "${CLAUDIO_PATH}/tmp/curl-config-XXXXXX") || {
        print_error "Failed to create temporary config file"
        exit 1
    }
    chmod 600 "$config_file"
    trap 'rm -f "$config_file"' RETURN

    printf 'url = "%s/%s"\n' "$WHATSAPP_API" "$phone_id" > "$config_file"
    printf 'header = "Authorization: Bearer %s"\n' "$access_token" >> "$config_file"

    test_result=$(curl -s --connect-timeout 10 --max-time 30 --config "$config_file")

    local verified_name
    verified_name=$(echo "$test_result" | jq -r '.verified_name // empty')
    if [ -z "$verified_name" ]; then
        print_error "Failed to verify WhatsApp credentials. Check your Phone Number ID and Access Token."
        exit 1
    fi

    print_success "Credentials verified: $verified_name"

    # Verify tunnel is configured
    if [ -z "$WEBHOOK_URL" ]; then
        print_warning "No tunnel configured. Run 'claudio install' first."
        exit 1
    fi

    # Save config: per-bot or global
    if [ -n "$bot_id" ]; then
        # Validate bot_id format
        if [[ ! "$bot_id" =~ ^[a-zA-Z0-9_-]+$ ]]; then
            print_error "Invalid bot name: '$bot_id'. Use only letters, numbers, hyphens, and underscores."
            exit 1
        fi

        local bot_dir="$CLAUDIO_PATH/bots/$bot_id"
        mkdir -p "$bot_dir"
        chmod 700 "$bot_dir"

        # Load existing config to preserve other platform's credentials
        export CLAUDIO_BOT_ID="$bot_id"
        export CLAUDIO_BOT_DIR="$bot_dir"
        export CLAUDIO_DB_FILE="$bot_dir/history.db"
        if [ -f "$bot_dir/bot.env" ]; then
            # shellcheck source=/dev/null
            source "$bot_dir/bot.env" 2>/dev/null || true
        fi

        # Re-apply new WhatsApp credentials (source may have overwritten them during re-configuration)
        export WHATSAPP_PHONE_NUMBER_ID="$phone_id"
        export WHATSAPP_ACCESS_TOKEN="$access_token"
        export WHATSAPP_APP_SECRET="$app_secret"
        export WHATSAPP_PHONE_NUMBER="$phone_number"
        export WHATSAPP_VERIFY_TOKEN="$verify_token"

        claudio_save_bot_env

        print_success "Bot config saved to $bot_dir/bot.env"
    else
        claudio_save_env
        print_success "Config saved to service.env"
    fi

    echo ""
    echo "=== Webhook Configuration ==="
    echo "Configure your WhatsApp webhook in Meta for Developers:"
    echo ""
    echo "  Callback URL: ${WEBHOOK_URL}/whatsapp/webhook"
    echo "  Verify Token: ${verify_token}"
    echo ""
    echo "Subscribe to these webhook fields:"
    echo "  - messages"
    echo ""
    print_success "Setup complete!"
}
