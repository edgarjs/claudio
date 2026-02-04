#!/bin/bash

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

_service_script_dir() {
    local src="${BASH_SOURCE[0]:-$0}"
    cd "$(dirname "$src")" && pwd
}
CLAUDIO_LIB="$(_service_script_dir)"
CLAUDIO_BIN="${CLAUDIO_LIB}/../claudio"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/com.claudio.server.plist"
SYSTEMD_UNIT="$HOME/.config/systemd/user/claudio.service"
SYSTEMD_SYSTEM_UNIT="/etc/systemd/system/claudio.service"
CRON_MARKER="# claudio-health-check"

is_root() {
    [ "$(id -u)" -eq 0 ]
}

validate_username() {
    local username="$1"
    if ! [[ "$username" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
        print_error "Invalid username. Use lowercase letters, digits, underscores, hyphens."
        exit 1
    fi
    if [ ${#username} -gt 32 ]; then
        print_error "Username too long (max 32 characters)."
        exit 1
    fi
}

create_system_user() {
    local username="$1"
    if id "$username" &>/dev/null; then
        print_success "User '$username' already exists."
    else
        echo "Creating system user '$username'..."
        useradd --system --shell /usr/sbin/nologin --home-dir "/home/$username" --create-home "$username"
        print_success "System user '$username' created."
    fi
}

deps_install() {
    echo "Checking dependencies..."

    # Check for missing package-manager dependencies
    local missing=()
    for cmd in sqlite3 jq; do
        if ! command -v "$cmd" > /dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done

    # Install missing packages via package manager
    if [ ${#missing[@]} -gt 0 ]; then
        echo "Missing: ${missing[*]}"
        if [[ "$(uname)" == "Darwin" ]]; then
            if ! command -v brew > /dev/null 2>&1; then
                print_error "Homebrew is required to install dependencies on macOS."
                echo "Install it from https://brew.sh/ then run 'claudio install' again."
                exit 1
            fi
            brew install "${missing[@]}"
        else
            if command -v apt-get > /dev/null 2>&1; then
                sudo apt-get update && sudo apt-get install -y "${missing[@]}"
            elif command -v dnf > /dev/null 2>&1; then
                sudo dnf install -y "${missing[@]}"
            elif command -v yum > /dev/null 2>&1; then
                sudo yum install -y "${missing[@]}"
            elif command -v pacman > /dev/null 2>&1; then
                sudo pacman -S --noconfirm "${missing[@]}"
            elif command -v apk > /dev/null 2>&1; then
                sudo apk add "${missing[@]}"
            else
                print_error "Could not detect package manager."
                echo "Please install manually: ${missing[*]}"
                exit 1
            fi
        fi

        for cmd in "${missing[@]}"; do
            if ! command -v "$cmd" > /dev/null 2>&1; then
                print_error "Failed to install $cmd."
                exit 1
            fi
        done
    fi

    # Install cloudflared (requires special handling on Linux)
    if ! command -v cloudflared > /dev/null 2>&1; then
        echo "Installing cloudflared..."
        if [[ "$(uname)" == "Darwin" ]]; then
            if ! command -v brew > /dev/null 2>&1; then
                print_error "Homebrew is required to install cloudflared on macOS."
                echo "Install it from https://brew.sh/ then run 'claudio install' again."
                exit 1
            fi
            brew install cloudflared
        else
            local arch
            arch=$(uname -m)
            case "$arch" in
                x86_64) arch="amd64" ;;
                aarch64|arm64) arch="arm64" ;;
            esac
            local url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"
            echo "Downloading from ${url}..."
            sudo curl -sL "$url" -o /usr/local/bin/cloudflared
            sudo chmod +x /usr/local/bin/cloudflared
        fi

        if ! command -v cloudflared > /dev/null 2>&1; then
            print_error "Failed to install cloudflared."
            exit 1
        fi
    fi

    print_success "All dependencies installed."
}

symlink_install() {
    local target_dir="$HOME/.local/bin"
    local target="$target_dir/claudio"

    mkdir -p "$target_dir"

    # Remove existing symlink or file
    if [ -L "$target" ] || [ -f "$target" ]; then
        rm -f "$target"
    fi

    ln -s "$CLAUDIO_BIN" "$target"
    print_success "Symlink created: $target -> $CLAUDIO_BIN"

    # Check if ~/.local/bin is in PATH
    if [[ ":$PATH:" != *":$target_dir:"* ]]; then
        path_add_to_profile "$target_dir"
    fi
}

path_add_to_profile() {
    local dir="$1"
    local export_line="export PATH=\"\$HOME/.local/bin:\$PATH\""
    local profile_file

    # Determine the shell profile file
    case "$(basename "$SHELL")" in
        zsh)  profile_file="$HOME/.zshrc" ;;
        bash)
            if [[ -f "$HOME/.bash_profile" ]]; then
                profile_file="$HOME/.bash_profile"
            else
                profile_file="$HOME/.bashrc"
            fi
            ;;
        *)    profile_file="$HOME/.profile" ;;
    esac

    # Check if already present in profile
    if [[ -f "$profile_file" ]] && grep -qE 'export\s+PATH=.*\.local/bin' "$profile_file"; then
        print_warning "$dir is not in your current PATH but is configured in $profile_file"
        echo "Restart your shell or run: source $profile_file"
        echo ""
        return
    fi

    # Add to profile
    echo "" >> "$profile_file"
    echo "# Added by claudio installer" >> "$profile_file"
    echo "$export_line" >> "$profile_file"

    print_success "Added $dir to PATH in $profile_file"
    echo "Restart your shell or run: source $profile_file"
    echo ""
}

symlink_uninstall() {
    local target="$HOME/.local/bin/claudio"
    if [ -L "$target" ]; then
        rm -f "$target"
        print_success "Symlink removed: $target"
    fi
}

symlink_install_system() {
    local target_user="$1"
    local target_home
    target_home=$(getent passwd "$target_user" | cut -d: -f6)
    local target_dir="$target_home/.local/bin"
    local target="$target_dir/claudio"

    mkdir -p "$target_dir"

    if [ -L "$target" ] || [ -f "$target" ]; then
        rm -f "$target"
    fi

    ln -s "$CLAUDIO_BIN" "$target"
    chown -R "$target_user:$target_user" "$target_home/.local"
    print_success "Symlink created: $target -> $CLAUDIO_BIN"
}

service_install() {
    local target_user="${1:-}"

    # Validate root/user requirements
    if is_root; then
        if [ -z "$target_user" ]; then
            print_error "Running as root requires the --user flag."
            echo ""
            echo "The Claude CLI cannot run as root for security reasons."
            echo "You must specify a non-root user to run the service."
            echo ""
            echo "Usage: claudio install --user <username>"
            echo ""
            echo "This creates a dedicated system user and installs a system-level service."
            exit 1
        fi
        if [[ "$(uname)" == "Darwin" ]]; then
            print_error "The --user flag is only supported on Linux."
            exit 1
        fi
        validate_username "$target_user"
        create_system_user "$target_user"

        # Set paths for target user
        local target_home
        target_home=$(getent passwd "$target_user" | cut -d: -f6)
        export CLAUDIO_PATH="$target_home/.claudio"
        export CLAUDIO_ENV_FILE="$CLAUDIO_PATH/service.env"
        export CLAUDIO_LOG_FILE="$CLAUDIO_PATH/claudio.log"
        export CLAUDIO_PROMPT_FILE="$CLAUDIO_PATH/SYSTEM_PROMPT.md"

        mkdir -p "$CLAUDIO_PATH"
        chown "$target_user:$target_user" "$CLAUDIO_PATH"
    elif [ -n "$target_user" ]; then
        print_error "The --user flag requires root privileges."
        echo "Run: sudo claudio install --user $target_user"
        exit 1
    fi

    deps_install

    if [ -n "$target_user" ]; then
        symlink_install_system "$target_user"
    else
        symlink_install
    fi

    claudio_init

    if [ -n "$target_user" ]; then
        cloudflared_setup_as_user "$target_user"
    else
        cloudflared_setup
    fi

    claudio_save_env

    # Set ownership after saving env (contains secrets)
    if [ -n "$target_user" ]; then
        chown -R "$target_user:$target_user" "$CLAUDIO_PATH"
        chmod 700 "$CLAUDIO_PATH"
        chmod 600 "$CLAUDIO_ENV_FILE"
    fi

    if [[ "$(uname)" == "Darwin" ]]; then
        service_install_launchd
    else
        if [ -n "$target_user" ]; then
            service_install_systemd_system "$target_user"
        else
            service_install_systemd
        fi
    fi

    if [ -n "$target_user" ]; then
        cron_install_system "$target_user"
    else
        cron_install
    fi

    echo ""
    print_success "Claudio service installed and started."
    # shellcheck disable=SC2016  # Backticks intentionally not expanded (documentation)
    echo 'Run `claudio telegram setup` to connect your Telegram bot.'
}

cloudflared_setup() {
    echo ""
    echo "Setting up Cloudflare tunnel (requires free Cloudflare account)..."
    # shellcheck disable=SC2034  # Used by claudio_save_env
    TUNNEL_TYPE="named"
    cloudflared_setup_named
}

cloudflared_setup_named() {
    echo ""

    # Check if already authenticated
    if [ -f "$HOME/.cloudflared/cert.pem" ]; then
        print_success "Cloudflare credentials found."
    else
        echo "Authenticating with Cloudflare (this will open your browser)..."
        if ! cloudflared tunnel login; then
            print_error "cloudflared login failed."
            exit 1
        fi
    fi

    echo ""
    read -rp "Enter a name for the tunnel (e.g. claudio): " tunnel_name
    if [ -z "$tunnel_name" ]; then
        tunnel_name="claudio"
    fi
    TUNNEL_NAME="$tunnel_name"

    # Create tunnel (ok if it already exists)
    local create_output
    if create_output=$(cloudflared tunnel create "$TUNNEL_NAME" 2>&1); then
        echo "$create_output"
    elif echo "$create_output" | grep -qi "already exists"; then
        print_success "Tunnel '$TUNNEL_NAME' already exists, reusing it."
    else
        print_error "Creating tunnel failed: $create_output"
        exit 1
    fi

    read -rp "Enter the hostname for this tunnel (e.g. claudio.example.com): " hostname
    if [ -z "$hostname" ]; then
        print_error "Hostname cannot be empty."
        exit 1
    fi
    # shellcheck disable=SC2034  # Used by claudio_save_env
    TUNNEL_HOSTNAME="$hostname"
    # shellcheck disable=SC2034  # Used by claudio_save_env
    WEBHOOK_URL="https://${hostname}"

    # Route DNS (ok if it already exists)
    local route_output
    if route_output=$(cloudflared tunnel route dns "$TUNNEL_NAME" "$hostname" 2>&1); then
        echo "$route_output"
    elif echo "$route_output" | grep -qi "already exists"; then
        print_success "DNS route for '${hostname}' already exists."
    else
        print_error "Routing DNS failed: $route_output"
        exit 1
    fi

    echo ""
    print_success "Named tunnel configured: https://${hostname}"
}

cloudflared_setup_as_user() {
    local target_user="$1"
    local target_home
    target_home=$(getent passwd "$target_user" | cut -d: -f6)
    local cloudflared_dir="$target_home/.cloudflared"

    echo ""
    echo "Setting up Cloudflare tunnel (requires free Cloudflare account)..."
    # shellcheck disable=SC2034  # Used by claudio_save_env
    TUNNEL_TYPE="named"

    # Create .cloudflared directory with proper ownership
    mkdir -p "$cloudflared_dir"
    chown "$target_user:$target_user" "$cloudflared_dir"
    chmod 700 "$cloudflared_dir"

    echo ""
    echo "Cloudflare tunnel authentication requires a web browser."
    echo "Options:"
    echo "  1) Run authentication now (requires display/browser access)"
    echo "  2) Skip and configure manually later"
    echo ""
    read -rp "Run cloudflared authentication now? [y/N]: " run_auth

    if [[ "$run_auth" =~ ^[Yy]$ ]]; then
        # Check if already authenticated
        if [ -f "$cloudflared_dir/cert.pem" ]; then
            print_success "Cloudflare credentials found."
        else
            echo "Authenticating with Cloudflare (this will open your browser)..."
            if ! sudo -u "$target_user" -H cloudflared tunnel login; then
                print_error "cloudflared login failed."
                echo "You can retry later with: sudo -u $target_user cloudflared tunnel login"
                exit 1
            fi
        fi

        echo ""
        read -rp "Enter a name for the tunnel (e.g. claudio): " tunnel_name
        if [ -z "$tunnel_name" ]; then
            tunnel_name="claudio"
        fi
        TUNNEL_NAME="$tunnel_name"

        # Create tunnel as target user
        local create_output
        if create_output=$(sudo -u "$target_user" -H cloudflared tunnel create "$TUNNEL_NAME" 2>&1); then
            echo "$create_output"
        elif echo "$create_output" | grep -qi "already exists"; then
            print_success "Tunnel '$TUNNEL_NAME' already exists, reusing it."
        else
            print_error "Creating tunnel failed: $create_output"
            exit 1
        fi

        read -rp "Enter the hostname for this tunnel (e.g. claudio.example.com): " hostname
        if [ -z "$hostname" ]; then
            print_error "Hostname cannot be empty."
            exit 1
        fi
        # shellcheck disable=SC2034  # Used by claudio_save_env
        TUNNEL_HOSTNAME="$hostname"
        # shellcheck disable=SC2034  # Used by claudio_save_env
        WEBHOOK_URL="https://${hostname}"

        # Route DNS as target user
        local route_output
        if route_output=$(sudo -u "$target_user" -H cloudflared tunnel route dns "$TUNNEL_NAME" "$hostname" 2>&1); then
            echo "$route_output"
        elif echo "$route_output" | grep -qi "already exists"; then
            print_success "DNS route for '${hostname}' already exists."
        else
            print_error "Routing DNS failed: $route_output"
            exit 1
        fi

        # Ensure credentials ownership
        chown -R "$target_user:$target_user" "$cloudflared_dir"

        echo ""
        print_success "Named tunnel configured: https://${hostname}"
    else
        echo ""
        print_warning "Cloudflare tunnel setup skipped."
        echo "To configure later, run as the service user:"
        echo "  sudo -u $target_user cloudflared tunnel login"
        echo "  sudo -u $target_user cloudflared tunnel create claudio"
        echo "  sudo -u $target_user cloudflared tunnel route dns claudio <hostname>"
        echo ""
        echo "Then update $CLAUDIO_ENV_FILE with:"
        echo "  TUNNEL_NAME=claudio"
        echo "  TUNNEL_HOSTNAME=<hostname>"
        echo "  WEBHOOK_URL=https://<hostname>"
        echo ""
    fi
}

service_install_launchd() {
    mkdir -p "$(dirname "$LAUNCHD_PLIST")"
    cat > "$LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claudio.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>${CLAUDIO_BIN}</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${CLAUDIO_PATH}/claudio.out.log</string>
    <key>StandardErrorPath</key>
    <string>${CLAUDIO_PATH}/claudio.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:${HOME}/.local/bin</string>
        <key>USER</key>
        <string>$(whoami)</string>
        <key>TERM</key>
        <string>dumb</string>
    </dict>
</dict>
</plist>
EOF
    launchctl stop com.claudio.server 2>/dev/null || true
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
    launchctl load "$LAUNCHD_PLIST"
    launchctl start com.claudio.server
}

service_install_systemd() {
    mkdir -p "$(dirname "$SYSTEMD_UNIT")"
    cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=Claudio - Telegram to Claude Code bridge
After=network.target

[Service]
Type=simple
ExecStart=${CLAUDIO_BIN} start
Restart=always
RestartSec=5
EnvironmentFile=${CLAUDIO_ENV_FILE}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin
Environment=HOME=${HOME}
Environment=USER=$(whoami)
Environment=TERM=dumb

[Install]
WantedBy=default.target
EOF
    systemctl --user stop claudio 2>/dev/null || true
    systemctl --user daemon-reload
    systemctl --user enable claudio
    systemctl --user start claudio
}

service_install_systemd_system() {
    local target_user="$1"
    local target_home
    target_home=$(getent passwd "$target_user" | cut -d: -f6)

    cat > "$SYSTEMD_SYSTEM_UNIT" <<EOF
[Unit]
Description=Claudio - Telegram to Claude Code bridge
After=network.target

[Service]
Type=simple
User=${target_user}
Group=${target_user}
ExecStart=${CLAUDIO_BIN} start
Restart=always
RestartSec=5
EnvironmentFile=${target_home}/.claudio/service.env
Environment=PATH=/usr/local/bin:/usr/bin:/bin:${target_home}/.local/bin
Environment=HOME=${target_home}
Environment=USER=${target_user}
Environment=TERM=dumb

[Install]
WantedBy=multi-user.target
EOF
    systemctl stop claudio 2>/dev/null || true
    systemctl daemon-reload
    systemctl enable claudio
    systemctl start claudio
}

service_uninstall() {
    local purge=false
    [ "$1" = "--purge" ] && purge=true

    if [[ "$(uname)" == "Darwin" ]]; then
        launchctl stop com.claudio.server 2>/dev/null || true
        launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
        rm -f "$LAUNCHD_PLIST"
    else
        # Check for system-level service first, then user-level
        if [ -f "$SYSTEMD_SYSTEM_UNIT" ]; then
            systemctl stop claudio 2>/dev/null || true
            systemctl disable claudio 2>/dev/null || true
            rm -f "$SYSTEMD_SYSTEM_UNIT"
            systemctl daemon-reload 2>/dev/null
        elif [ -f "$SYSTEMD_UNIT" ]; then
            systemctl --user stop claudio 2>/dev/null || true
            systemctl --user disable claudio 2>/dev/null || true
            rm -f "$SYSTEMD_UNIT"
            systemctl --user daemon-reload 2>/dev/null
        fi
    fi

    cron_uninstall
    symlink_uninstall
    print_success "Claudio service removed."

    if [ "$purge" = true ]; then
        rm -rf "$CLAUDIO_PATH"
        print_success "Removed ${CLAUDIO_PATH}"
    fi
}

service_restart() {
    if [[ "$(uname)" == "Darwin" ]]; then
        launchctl stop com.claudio.server 2>/dev/null || true
        launchctl start com.claudio.server
    else
        if [ -f "$SYSTEMD_SYSTEM_UNIT" ]; then
            systemctl restart claudio
        else
            systemctl --user restart claudio
        fi
    fi
    print_success "Claudio service restarted."
}

service_status() {
    echo "=== Claudio Status ==="
    echo ""

    # Check service status
    local service_running=false
    if [[ "$(uname)" == "Darwin" ]]; then
        if launchctl list 2>/dev/null | grep -q "com.claudio.server"; then
            local pid
            pid=$(launchctl list | grep "com.claudio.server" | awk '{print $1}')
            # If PID is a number (not "-"), service is running
            if [[ "$pid" =~ ^[0-9]+$ ]]; then
                service_running=true
                echo "Service:  ✅ Running (PID: $pid)"
            else
                echo "Service:  ❌ Stopped"
            fi
        else
            echo "Service:  ❌ Not installed"
        fi
    else
        # Check system-level service first, then user-level
        if [ -f "$SYSTEMD_SYSTEM_UNIT" ]; then
            if systemctl is-active --quiet claudio 2>/dev/null; then
                service_running=true
                echo "Service:  ✅ Running (system)"
            else
                echo "Service:  ❌ Stopped (system)"
            fi
        elif [ -f "$SYSTEMD_UNIT" ]; then
            if systemctl --user is-active --quiet claudio 2>/dev/null; then
                service_running=true
                echo "Service:  ✅ Running (user)"
            else
                echo "Service:  ❌ Stopped (user)"
            fi
        else
            echo "Service:  ❌ Not installed"
        fi
    fi

    # Check health endpoint
    if [ "$service_running" = true ]; then
        local health
        health=$(curl -s "http://localhost:${PORT:-8421}/health" 2>/dev/null || echo '{}')
        local webhook_status
        webhook_status=$(echo "$health" | jq -r '.checks.telegram_webhook.status // "unknown"' 2>/dev/null)

        if [ "$webhook_status" = "ok" ]; then
            echo "Webhook:  ✅ Registered"
        elif [ "$webhook_status" = "mismatch" ]; then
            local expected actual
            expected=$(echo "$health" | jq -r '.checks.telegram_webhook.expected // "unknown"' 2>/dev/null)
            actual=$(echo "$health" | jq -r '.checks.telegram_webhook.actual // "none"' 2>/dev/null)
            echo "Webhook:  ❌ Mismatch"
            echo "          Expected: $expected"
            echo "          Actual:   $actual"
        else
            echo "Webhook:  ❌ Not registered"
        fi
    else
        echo "Webhook:  ⚠️  Unknown (service not running)"
    fi

    # Show tunnel info
    echo ""
    if [ -n "$TUNNEL_NAME" ]; then
        echo "Tunnel:   $TUNNEL_NAME"
    fi
    if [ -n "$WEBHOOK_URL" ]; then
        echo "URL:      $WEBHOOK_URL"
    fi

    echo ""
}

cron_install() {
    local health_script="${CLAUDIO_LIB}/health-check.sh"
    local cron_entry="*/5 * * * * ${health_script} ${CRON_MARKER}"

    # Remove existing entry if present, then add new one
    (crontab -l 2>/dev/null | grep -v "$CRON_MARKER"; echo "$cron_entry") | crontab -
    print_success "Health check cron job installed (runs every 5 minutes)."
}

cron_install_system() {
    local target_user="$1"
    local health_script="${CLAUDIO_LIB}/health-check.sh"
    local cron_entry="*/5 * * * * ${health_script} ${CRON_MARKER}"

    # Install crontab for the target user
    (sudo -u "$target_user" crontab -l 2>/dev/null | grep -v "$CRON_MARKER"; echo "$cron_entry") | sudo -u "$target_user" crontab -
    print_success "Health check cron job installed for user $target_user (runs every 5 minutes)."
}

cron_uninstall() {
    if crontab -l 2>/dev/null | grep -q "$CRON_MARKER"; then
        crontab -l 2>/dev/null | grep -v "$CRON_MARKER" | crontab -
        print_success "Health check cron job removed."
    fi
}

service_update() {
    # Get the project root directory (parent of lib/)
    local project_dir
    project_dir="$(cd "$CLAUDIO_LIB/.." && pwd)"

    # Check if it's a git repository
    if [ ! -d "$project_dir/.git" ]; then
        print_error "Not a git repository: $project_dir"
        echo "Updates require the original cloned repository."
        exit 1
    fi

    echo "Checking for updates in $project_dir..."

    # Fetch and check for updates
    if ! git -C "$project_dir" fetch origin main 2>/dev/null; then
        print_error "Failed to fetch updates. Check your internet connection."
        exit 1
    fi

    local local_hash remote_hash
    local_hash=$(git -C "$project_dir" rev-parse HEAD)
    remote_hash=$(git -C "$project_dir" rev-parse origin/main)

    if [ "$local_hash" = "$remote_hash" ]; then
        print_success "Already up to date."
        return 0
    fi

    echo "Updating from $(echo "$local_hash" | cut -c1-7) to $(echo "$remote_hash" | cut -c1-7)..."

    if ! git -C "$project_dir" pull --ff-only origin main; then
        print_error "Failed to update. You may have local changes."
        echo "Run 'git -C $project_dir status' to check."
        exit 1
    fi

    print_success "Claudio updated successfully."
    service_restart
}
