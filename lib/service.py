"""Service management for Claudio — install, uninstall, update, restart.

Ports lib/service.sh (660 lines) + lib/server.sh (145 lines) to Python.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from lib.util import print_error, print_success, print_warning

LAUNCHD_PLIST = os.path.expanduser(
    "~/Library/LaunchAgents/com.claudio.server.plist")
SYSTEMD_UNIT = os.path.expanduser(
    "~/.config/systemd/user/claudio.service")
CRON_MARKER = "# claudio-health-check"


def _is_darwin():
    return platform.system() == "Darwin"


def _project_dir():
    """Return the project root (parent of lib/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _claudio_bin():
    """Return path to the claudio CLI entry point."""
    return os.path.join(_project_dir(), "claudio")


# -- Dependency installation --


def deps_install():
    """Check and install required dependencies."""
    print("Checking dependencies...")

    # Check for missing package-manager dependencies
    missing = []
    for cmd in ("sqlite3", "jq"):
        if shutil.which(cmd) is None:
            missing.append(cmd)

    if missing:
        print(f"Missing: {' '.join(missing)}")
        if _is_darwin():
            if shutil.which("brew") is None:
                print_error(
                    "Homebrew is required to install dependencies on macOS.")
                print(
                    "Install it from https://brew.sh/ then run 'claudio install' again.")
                sys.exit(1)
            subprocess.run(["brew", "install"] + missing, check=True)
        else:
            installed = False
            for pkg_mgr, args in [
                ("apt-get", ["sudo", "apt-get", "update"]),
                ("dnf", None),
                ("yum", None),
                ("pacman", None),
                ("apk", None),
            ]:
                if shutil.which(pkg_mgr) is not None:
                    if args:
                        subprocess.run(args, check=True)
                    install_cmd = {
                        "apt-get": ["sudo", "apt-get", "install", "-y"],
                        "dnf": ["sudo", "dnf", "install", "-y"],
                        "yum": ["sudo", "yum", "install", "-y"],
                        "pacman": ["sudo", "pacman", "-S", "--noconfirm"],
                        "apk": ["sudo", "apk", "add"],
                    }[pkg_mgr]
                    subprocess.run(install_cmd + missing, check=True)
                    installed = True
                    break
            if not installed:
                print_error("Could not detect package manager.")
                print(f"Please install manually: {' '.join(missing)}")
                sys.exit(1)

        for cmd in missing:
            if shutil.which(cmd) is None:
                print_error(f"Failed to install {cmd}.")
                sys.exit(1)

    # Install cloudflared
    if shutil.which("cloudflared") is None:
        print("Installing cloudflared...")
        if _is_darwin():
            if shutil.which("brew") is None:
                print_error(
                    "Homebrew is required to install cloudflared on macOS.")
                print(
                    "Install it from https://brew.sh/ then run 'claudio install' again.")
                sys.exit(1)
            subprocess.run(["brew", "install", "cloudflared"], check=True)
        else:
            arch = platform.machine()
            arch_map = {
                "x86_64": "amd64",
                "aarch64": "arm64",
                "arm64": "arm64",
                "armv7l": "arm",
            }
            arch = arch_map.get(arch, arch)
            url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
            print(f"Downloading from {url}...")
            try:
                fd, tmp = tempfile.mkstemp()
                os.close(fd)
                subprocess.run(
                    ["curl", "-fSL", url, "-o", tmp], check=True)
                if os.path.getsize(tmp) == 0:
                    os.unlink(tmp)
                    print_error("Downloaded cloudflared binary is empty")
                    sys.exit(1)
                subprocess.run(
                    ["sudo", "mv", "-f", tmp, "/usr/local/bin/cloudflared"],
                    check=True)
                subprocess.run(
                    ["sudo", "chmod", "+x", "/usr/local/bin/cloudflared"],
                    check=True)
            except (subprocess.CalledProcessError, OSError):
                if os.path.exists(tmp):
                    os.unlink(tmp)
                print_error(f"Failed to download cloudflared from {url}")
                sys.exit(1)

        if shutil.which("cloudflared") is None:
            print_error("Failed to install cloudflared.")
            sys.exit(1)

    # Install Python dependencies for memory system
    try:
        __import__("fastembed")
    except ImportError:
        print("Installing fastembed (memory system)...")
        pip_args = [sys.executable, "-m", "pip",
                    "install", "--user", "fastembed"]
        # Check for PEP 668 externally-managed environments
        check = subprocess.run(
            [sys.executable, "-m", "pip", "install",
                "--user", "--dry-run", "fastembed"],
            capture_output=True, text=True)
        if "externally-managed-environment" in check.stderr:
            pip_args = [sys.executable, "-m", "pip", "install",
                        "--user", "--break-system-packages", "fastembed"]
        result = subprocess.run(pip_args)
        if result.returncode == 0:
            print_success("fastembed installed.")
        else:
            print_warning(
                "Failed to install fastembed — memory system will be disabled.")
            print_warning(
                "Install manually with: pip3 install --user --break-system-packages fastembed")

    print_success("All dependencies installed.")


# -- Symlink management --


def symlink_install():
    """Create ~/.local/bin/claudio symlink."""
    target_dir = os.path.expanduser("~/.local/bin")
    target = os.path.join(target_dir, "claudio")
    os.makedirs(target_dir, exist_ok=True)

    if os.path.islink(target) or os.path.isfile(target):
        os.unlink(target)

    os.symlink(_claudio_bin(), target)
    print_success(f"Symlink created: {target} -> {_claudio_bin()}")

    if target_dir not in os.environ.get("PATH", "").split(":"):
        print_warning(f"{target_dir} is not in your PATH.")
        print("Add this line to your shell profile (~/.bashrc, ~/.zshrc, etc.):")
        print('  export PATH="$HOME/.local/bin:$PATH"')
        print()


def symlink_uninstall():
    """Remove ~/.local/bin/claudio symlink."""
    target = os.path.expanduser("~/.local/bin/claudio")
    if os.path.islink(target):
        os.unlink(target)
        print_success(f"Symlink removed: {target}")


# -- Cloudflared tunnel setup --


def cloudflared_setup(config):
    """Interactive named tunnel setup.

    Args:
        config: ClaudioConfig instance (will be modified with tunnel details).
    """
    print()
    print("Setting up Cloudflare tunnel (requires free Cloudflare account)...")
    print()

    # Check if already authenticated
    cert_path = os.path.expanduser("~/.cloudflared/cert.pem")
    if os.path.isfile(cert_path):
        print_success("Cloudflare credentials found.")
    else:
        print("Authenticating with Cloudflare (this will open your browser)...")
        result = subprocess.run(["cloudflared", "tunnel", "login"])
        if result.returncode != 0:
            print_error("cloudflared login failed.")
            sys.exit(1)

    print()
    tunnel_name = input(
        "Enter a name for the tunnel (e.g. claudio): ").strip()
    if not tunnel_name:
        tunnel_name = "claudio"
    config.tunnel_name = tunnel_name

    # Create tunnel (ok if it already exists)
    result = subprocess.run(
        ["cloudflared", "tunnel", "create", tunnel_name],
        capture_output=True, text=True)
    if result.returncode != 0:
        if "already exists" in result.stderr.lower():
            print_success(
                f"Tunnel '{tunnel_name}' already exists, reusing it.")
        else:
            print_error(f"Creating tunnel failed: {result.stderr}")
            sys.exit(1)
    else:
        print(result.stdout)

    hostname = input(
        "Enter the hostname for this tunnel (e.g. claudio.example.com): ").strip()
    if not hostname:
        print_error("Hostname cannot be empty.")
        sys.exit(1)

    config.tunnel_hostname = hostname
    config.webhook_url = f"https://{hostname}"

    # Route DNS (ok if it already exists)
    result = subprocess.run(
        ["cloudflared", "tunnel", "route", "dns", tunnel_name, hostname],
        capture_output=True, text=True)
    if result.returncode != 0:
        if "already exists" in result.stderr.lower():
            print_success(f"DNS route for '{hostname}' already exists.")
        else:
            print_error(f"Routing DNS failed: {result.stderr}")
            sys.exit(1)
    else:
        print(result.stdout)

    print()
    print_success(f"Named tunnel configured: https://{hostname}")


# -- Service file generation --


def service_install_systemd(config):
    """Write systemd unit file and enable/start the service."""
    unit_dir = os.path.dirname(SYSTEMD_UNIT)
    os.makedirs(unit_dir, exist_ok=True)

    claudio_bin = _claudio_bin()
    env_file = config.env_file
    home = os.path.expanduser("~")
    user = os.environ.get("USER", os.getlogin())

    unit_content = f"""\
[Unit]
Description=Claudio - Telegram to Claude Code bridge
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
ExecStart={claudio_bin} start
Restart=always
RestartSec=5
TimeoutStopSec=1800
KillMode=mixed
EnvironmentFile={env_file}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:{home}/.local/bin
Environment=HOME={home}
Environment=USER={user}
Environment=TERM=dumb

[Install]
WantedBy=default.target
"""

    with open(SYSTEMD_UNIT, "w") as f:
        f.write(unit_content)

    subprocess.run(
        ["systemctl", "--user", "stop", "claudio"],
        capture_output=True)
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "claudio"], check=True)
    subprocess.run(
        ["systemctl", "--user", "start", "claudio"], check=True)

    _enable_linger()


def service_install_launchd(config):
    """Write launchd plist and load/start the service."""
    plist_dir = os.path.dirname(LAUNCHD_PLIST)
    os.makedirs(plist_dir, exist_ok=True)

    claudio_bin = _claudio_bin()
    claudio_path = config.claudio_path
    home = os.path.expanduser("~")
    user = os.environ.get("USER", os.getlogin())

    plist_content = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claudio.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>{claudio_bin}</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{claudio_path}/claudio.out.log</string>
    <key>StandardErrorPath</key>
    <string>{claudio_path}/claudio.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:{home}/.local/bin</string>
        <key>USER</key>
        <string>{user}</string>
        <key>TERM</key>
        <string>dumb</string>
    </dict>
</dict>
</plist>
"""

    subprocess.run(
        ["launchctl", "stop", "com.claudio.server"],
        capture_output=True)
    subprocess.run(
        ["launchctl", "unload", LAUNCHD_PLIST],
        capture_output=True)

    with open(LAUNCHD_PLIST, "w") as f:
        f.write(plist_content)

    subprocess.run(["launchctl", "load", LAUNCHD_PLIST], check=True)
    subprocess.run(
        ["launchctl", "start", "com.claudio.server"], check=True)


# -- Linger management --


def _enable_linger():
    """Enable loginctl linger so user services survive logout."""
    if shutil.which("loginctl"):
        subprocess.run(
            ["loginctl", "enable-linger", os.environ.get("USER", "")],
            capture_output=True)


def _disable_linger():
    """Disable loginctl linger if no user services remain."""
    if shutil.which("loginctl"):
        result = subprocess.run(
            ["systemctl", "--user", "list-unit-files",
                "--state=enabled", "--no-legend"],
            capture_output=True, text=True)
        if result.returncode == 0:
            remaining = len(
                [x for x in result.stdout.strip().split('\n') if x.strip()])
            if remaining == 0:
                subprocess.run(
                    ["loginctl", "disable-linger",
                        os.environ.get("USER", "")],
                    capture_output=True)


# -- Claude hooks --


def claude_hooks_install(project_dir):
    """Register PostToolUse hook in ~/.claude/settings.json."""
    settings_file = os.path.expanduser("~/.claude/settings.json")
    hook_cmd = f'python3 "{project_dir}/lib/hooks/post-tool-use.py"'

    os.makedirs(os.path.dirname(settings_file), exist_ok=True)

    # Load existing settings
    settings = {}
    if os.path.isfile(settings_file):
        try:
            with open(settings_file) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            settings = {}

    # Check if hook already registered
    hooks = settings.get("hooks", {})
    post_tool_use = hooks.get("PostToolUse", [])
    for entry in post_tool_use:
        for h in entry.get("hooks", []):
            if h.get("command") == hook_cmd:
                return  # Already registered

    # Add the hook
    new_entry = {"hooks": [{"type": "command", "command": hook_cmd}]}
    post_tool_use.append(new_entry)
    hooks["PostToolUse"] = post_tool_use
    settings["hooks"] = hooks

    with open(settings_file, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print(f"Registered PostToolUse hook in {settings_file}")


# -- Health-check cron --


def cron_install(config):
    """Install health-check cron job (runs every minute)."""
    health_script = os.path.join(_project_dir(), "lib", "health_check.py")
    claudio_path = config.claudio_path
    home = os.path.expanduser("~")

    cron_entry = (
        f"* * * * * export PATH=/usr/local/bin:/usr/bin:/bin:{home}/.local/bin:$PATH"
        f" && {sys.executable} {health_script}"
        f" >> {claudio_path}/cron.log 2>&1 {CRON_MARKER}"
    )

    # Remove existing entry, add new one
    existing = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True)
    lines = existing.stdout.strip().split(
        '\n') if existing.returncode == 0 else []
    lines = [x for x in lines if CRON_MARKER not in x]
    lines.append(cron_entry)

    proc = subprocess.run(
        ["crontab", "-"], input='\n'.join(lines) + '\n',
        text=True, capture_output=True)
    if proc.returncode == 0:
        print_success("Health check cron job installed (runs every minute).")
    else:
        print_warning(f"Failed to install cron job: {proc.stderr}")


def cron_uninstall():
    """Remove health-check cron job."""
    existing = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True)
    if existing.returncode != 0:
        return

    if CRON_MARKER not in existing.stdout:
        return

    lines = [x for x in existing.stdout.strip().split(
        '\n') if CRON_MARKER not in x]
    subprocess.run(
        ["crontab", "-"], input='\n'.join(lines) + '\n' if lines else '',
        text=True, capture_output=True)
    print_success("Health check cron job removed.")


# -- Webhook registration --


def register_webhook(tunnel_url, token, secret='', chat_id='',
                     retry_delay=60, max_retries=10):
    """Register a Telegram webhook with retry logic."""
    webhook_url = f"{tunnel_url}/telegram/webhook"

    for attempt in range(1, max_retries + 1):
        try:
            data = urllib.parse.urlencode({
                "url": webhook_url,
                "allowed_updates": '["message"]',
                **({"secret_token": secret} if secret else {}),
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/setWebhook",
                data=data)
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            result = {"ok": False, "description": str(e)}

        if result.get("ok"):
            print_success("Webhook registered successfully.")
            # Notify user via Telegram
            if chat_id:
                try:
                    msg_data = urllib.parse.urlencode({
                        "chat_id": chat_id,
                        "text": "Webhook registered! We can chat now. What are you up to?",
                    }).encode()
                    msg_req = urllib.request.Request(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data=msg_data)
                    urllib.request.urlopen(msg_req, timeout=10)
                except (urllib.error.URLError, OSError):
                    pass  # Non-critical
            return True

        desc = result.get("description", "Unknown error")

        if attempt < max_retries:
            print(f" Retrying in {retry_delay}s... "
                  f"(attempt {attempt}/{max_retries}: {desc})")
            # Countdown for interactive, simple sleep for non-interactive
            if sys.stdin.isatty():
                for i in range(retry_delay, 0, -1):
                    print(f"\r Retrying in {i}s... ", end="", flush=True)
                    time.sleep(1)
                print("\r" + " " * 40 + "\r", end="")
            else:
                time.sleep(retry_delay)

    print_error(f"Webhook registration failed after {max_retries} attempts.")
    return False


def register_all_webhooks(config, tunnel_url):
    """Register webhooks for all configured bots."""
    bot_ids = config.list_bots()
    if not bot_ids:
        return

    for bot_id in bot_ids:
        bot = config.load_bot(bot_id)
        if not bot.telegram_token:
            continue
        print(f"Registering webhook for bot '{bot_id}'...")
        retry_delay = int(config.env.get('WEBHOOK_RETRY_DELAY', '60'))
        register_webhook(
            tunnel_url, bot.telegram_token, bot.webhook_secret,
            bot.telegram_chat_id, retry_delay=retry_delay)


# -- Service lifecycle --


def service_install(config, bot_id='claudio'):
    """Full install: deps, symlink, tunnel, service unit, cron, hooks, bot setup."""
    from lib.setup import bot_setup

    # Validate bot_id
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', bot_id):
        print_error(
            f"Invalid bot name: '{bot_id}'. "
            "Use only letters, numbers, hyphens, and underscores.")
        sys.exit(1)

    deps_install()
    symlink_install()
    config.init()

    # System setup (idempotent): cloudflared tunnel, service unit, cron, hooks
    if not config.tunnel_name:
        cloudflared_setup(config)
        config.save_service_env()

    if _is_darwin():
        service_install_launchd(config)
    else:
        service_install_systemd(config)

    cron_install(config)
    claude_hooks_install(_project_dir())

    # Per-bot setup
    bot_setup(config, bot_id)

    # Restart to pick up new bot
    print()
    print(f"Restarting service to pick up bot '{bot_id}'...")
    try:
        service_restart()
    except Exception:
        pass

    print()
    print_success(f"Claudio service installed with bot '{bot_id}'.")


def service_uninstall(config, arg):
    """Uninstall a bot or purge entire installation."""
    if not arg:
        print_error(
            "Error: 'uninstall' requires an argument. "
            "Usage: claudio uninstall <bot_name> | --purge")
        sys.exit(1)

    if arg == "--purge":
        if _is_darwin():
            subprocess.run(
                ["launchctl", "stop", "com.claudio.server"],
                capture_output=True)
            subprocess.run(
                ["launchctl", "unload", LAUNCHD_PLIST],
                capture_output=True)
            if os.path.isfile(LAUNCHD_PLIST):
                os.unlink(LAUNCHD_PLIST)
        else:
            subprocess.run(
                ["systemctl", "--user", "stop", "claudio"],
                capture_output=True)
            subprocess.run(
                ["systemctl", "--user", "disable", "claudio"],
                capture_output=True)
            if os.path.isfile(SYSTEMD_UNIT):
                os.unlink(SYSTEMD_UNIT)
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True)
            _disable_linger()

        cron_uninstall()
        symlink_uninstall()
        print_success("Claudio service removed.")

        if os.path.isdir(config.claudio_path):
            shutil.rmtree(config.claudio_path)
            print_success(f"Removed {config.claudio_path}")
        return

    # Per-bot uninstall
    import re
    bot_id = arg
    if not re.match(r'^[a-zA-Z0-9_-]+$', bot_id):
        print_error(
            f"Invalid bot name: '{bot_id}'. "
            "Use only letters, numbers, hyphens, and underscores.")
        sys.exit(1)

    bot_dir = os.path.join(config.claudio_path, "bots", bot_id)
    if not os.path.isdir(bot_dir):
        print_error(f"Bot '{bot_id}' not found at {bot_dir}")
        sys.exit(1)

    print(f"This will remove bot '{bot_id}' and all its data:")
    print(f"  {bot_dir}/")
    confirm = input("Continue? [y/N] ").strip()
    if not confirm.lower().startswith('y'):
        print("Cancelled.")
        return

    shutil.rmtree(bot_dir)
    print_success(f"Bot '{bot_id}' removed.")

    # Restart service to drop the bot
    try:
        service_restart()
    except Exception:
        pass


def service_restart():
    """Restart the Claudio service."""
    if _is_darwin():
        subprocess.run(
            ["launchctl", "stop", "com.claudio.server"],
            capture_output=True)
        subprocess.run(
            ["launchctl", "start", "com.claudio.server"], check=True)
    else:
        subprocess.run(
            ["systemctl", "--user", "restart", "claudio"], check=True)
    print_success("Claudio service restarted.")


def service_status(config):
    """Show service and webhook status."""
    print("=== Claudio Status ===")
    print()

    service_running = False
    if _is_darwin():
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True)
        if "com.claudio.server" in result.stdout:
            for line in result.stdout.split('\n'):
                if "com.claudio.server" in line:
                    pid = line.split()[0]
                    if pid.isdigit():
                        service_running = True
                        print(f"Service:  \u2705 Running (PID: {pid})")
                    else:
                        print("Service:  \u274c Stopped")
                    break
        else:
            print("Service:  \u274c Not installed")
    else:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "claudio"],
            capture_output=True)
        if result.returncode == 0:
            service_running = True
            print("Service:  \u2705 Running")
        else:
            check = subprocess.run(
                ["systemctl", "--user", "list-unit-files"],
                capture_output=True, text=True)
            if "claudio" in check.stdout:
                print("Service:  \u274c Stopped")
            else:
                print("Service:  \u274c Not installed")

    # Check health endpoint
    if service_running:
        try:
            port = config.port
            req = urllib.request.Request(
                f"http://localhost:{port}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                health = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            health = {}

        webhook_status = (health.get("checks", {})
                          .get("telegram_webhook", {})
                          .get("status", "unknown"))

        if webhook_status == "ok":
            print("Webhook:  \u2705 Registered")
        elif webhook_status == "mismatch":
            expected = (health.get("checks", {})
                        .get("telegram_webhook", {})
                        .get("expected", "unknown"))
            actual = (health.get("checks", {})
                      .get("telegram_webhook", {})
                      .get("actual", "none"))
            print("Webhook:  \u274c Mismatch")
            print(f"          Expected: {expected}")
            print(f"          Actual:   {actual}")
        elif webhook_status == "unknown":
            print("Webhook:  \u26a0\ufe0f  Unknown (could not parse health response)")
        else:
            print("Webhook:  \u274c Not registered")
    else:
        print("Webhook:  \u26a0\ufe0f  Unknown (service not running)")

    # Show tunnel info
    print()
    if config.tunnel_name:
        print(f"Tunnel:   {config.tunnel_name}")
    if config.webhook_url:
        print(f"URL:      {config.webhook_url}")
    print()


def service_update(config):
    """Update to the latest release via git pull."""
    project_dir = _project_dir()

    if not os.path.isdir(os.path.join(project_dir, ".git")):
        print_error(f"Not a git repository: {project_dir}")
        print("Updates require the original cloned repository.")
        sys.exit(1)

    print(f"Checking for updates in {project_dir}...")

    result = subprocess.run(
        ["git", "-C", project_dir, "fetch", "origin", "main"],
        capture_output=True)
    if result.returncode != 0:
        print_error(
            "Failed to fetch updates. Check your internet connection.")
        sys.exit(1)

    local_hash = subprocess.run(
        ["git", "-C", project_dir, "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    remote_hash = subprocess.run(
        ["git", "-C", project_dir, "rev-parse", "origin/main"],
        capture_output=True, text=True).stdout.strip()

    if local_hash == remote_hash:
        print_success("Already up to date.")
        return

    print(
        f"Updating from {local_hash[:7]} to {remote_hash[:7]}...")

    result = subprocess.run(
        ["git", "-C", project_dir, "pull", "--ff-only", "origin", "main"],
        capture_output=True, text=True)
    if result.returncode != 0:
        print_error("Failed to update. You may have local changes.")
        print(f"Run 'git -C {project_dir} status' to check.")
        sys.exit(1)

    print_success("Claudio updated successfully.")

    claude_hooks_install(project_dir)

    # Ensure linger is enabled for existing installs
    if not _is_darwin():
        _enable_linger()

    service_restart()


def server_start(config):
    """Start the Python HTTP server (exec replaces current process).

    Uses os.execvp so the Python server gets PID 1 for systemd.
    """
    server_py = os.path.join(_project_dir(), "lib", "server.py")
    os.environ["PORT"] = str(config.port)
    os.execvp(sys.executable, [sys.executable, server_py])
