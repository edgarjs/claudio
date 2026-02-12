#!/usr/bin/env python3
"""Health check for Claudio -- runs via cron every minute.

Checks if the Claudio server is up, auto-restarts if down (throttled),
and performs additional system checks when healthy:
  - Disk usage alerts (configurable threshold, default 90%)
  - Log rotation (configurable max size, default 10MB)
  - Backup freshness (alerts if last backup is older than threshold)
  - Recent log analysis (scans for errors, rapid restarts, API slowness)
"""

import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

MAX_RESTART_ATTEMPTS = 3
MIN_RESTART_INTERVAL = 180  # 3 minutes in seconds

# Timestamp format used in log lines: [YYYY-MM-DD HH:MM:SS]
_LOG_TS_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')

# Backup directory name format: YYYY-MM-DD_HHMM
_BACKUP_DIR_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})$')


def _parse_env_file(path):
    """Parse a KEY="value" or KEY=value env file.

    Self-contained duplicate of config.parse_env_file() to avoid import path
    issues when this script runs from cron with a minimal PATH/PYTHONPATH.
    """
    result = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                eq = line.find('=')
                if eq < 1:
                    continue
                key = line[:eq]
                if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key):
                    continue
                val = line[eq + 1:]
                if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                    val = val.replace('\\n', '\n')
                    val = val.replace('\\`', '`')
                    val = val.replace('\\$', '$')
                    val = val.replace('\\"', '"')
                    val = val.replace('\\\\', '\\')
                result[key] = val
    except (OSError, IOError):
        pass
    return result


class HealthChecker:
    """Claudio health checker -- monitors service health and system state."""

    def __init__(self, claudio_path=None):
        self.claudio_path = claudio_path or os.path.join(
            os.path.expanduser('~'), '.claudio')
        self.env_file = os.path.join(self.claudio_path, 'service.env')
        self.log_file = os.path.join(self.claudio_path, 'claudio.log')
        self.restart_stamp = os.path.join(
            self.claudio_path, '.last_restart_attempt')
        self.fail_count_file = os.path.join(
            self.claudio_path, '.restart_fail_count')
        self.log_alert_stamp = os.path.join(
            self.claudio_path, '.last_log_alert')

        # Config values (defaults, overridden by _load_config)
        self.port = 8421
        self.telegram_token = ''
        self.telegram_chat_id = ''
        self.disk_threshold = 90
        self.log_max_size = 10485760  # 10MB
        self.backup_max_age = 7200  # 2 hours
        self.backup_dest = '/mnt/ssd'
        self.log_check_window = 300  # 5 minutes
        self.log_alert_cooldown = 1800  # 30 minutes

    def run(self):
        """Main entry point. Returns 0 for healthy, 1 for unhealthy/error."""
        if not os.path.isfile(self.env_file):
            self._log_error(
                f"Environment file not found: {self.env_file}")
            return 1

        self._load_config()

        # Ensure XDG_RUNTIME_DIR is set on Linux (cron doesn't provide it,
        # needed for systemctl --user)
        if platform.system() != 'Darwin':
            if 'XDG_RUNTIME_DIR' not in os.environ:
                os.environ['XDG_RUNTIME_DIR'] = (
                    f"/run/user/{os.getuid()}")

        http_code, body = self._check_health_endpoint()

        if http_code == 200:
            self._handle_healthy(body)
            return 0
        elif http_code == 503:
            self._log_error(
                f"Health check returned unhealthy: {body}")
            return 1
        elif http_code == 0:
            self._handle_connection_refused()
            return 1
        else:
            self._log_error(
                f"Unexpected response (HTTP {http_code}): {body}")
            return 1

    def _load_config(self):
        """Load service.env + first bot's config for alert credentials."""
        env = _parse_env_file(self.env_file)
        self.port = int(env.get('PORT', '8421'))
        self.disk_threshold = int(
            env.get('DISK_USAGE_THRESHOLD', '90'))
        self.log_max_size = int(
            env.get('LOG_MAX_SIZE', '10485760'))
        self.backup_max_age = int(
            env.get('BACKUP_MAX_AGE', '7200'))
        self.backup_dest = env.get('BACKUP_DEST', '/mnt/ssd')
        self.log_check_window = int(
            env.get('LOG_CHECK_WINDOW', '300'))
        self.log_alert_cooldown = int(
            env.get('LOG_ALERT_COOLDOWN', '1800'))

        # Load first bot's config for Telegram alert credentials
        bots_dir = os.path.join(self.claudio_path, 'bots')
        if os.path.isdir(bots_dir):
            for name in sorted(os.listdir(bots_dir)):
                bot_env_path = os.path.join(bots_dir, name, 'bot.env')
                if os.path.isfile(bot_env_path):
                    bot_env = _parse_env_file(bot_env_path)
                    self.telegram_token = bot_env.get(
                        'TELEGRAM_BOT_TOKEN', '')
                    self.telegram_chat_id = bot_env.get(
                        'TELEGRAM_CHAT_ID', '')
                    break

    def _check_health_endpoint(self):
        """Call the /health endpoint. Returns (http_code, body).

        Returns http_code=0 on connection refused/timeout.
        """
        url = f"http://localhost:{self.port}/health"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode('utf-8', errors='replace')
                return (resp.code, body)
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode('utf-8', errors='replace')
            except Exception:
                pass
            return (e.code, body)
        except (urllib.error.URLError, OSError):
            return (0, '')

    def _handle_healthy(self, body):
        """Service is healthy (200). Clear fail state, run additional checks."""
        self._clear_fail_state()

        # Log pending updates if any
        try:
            data = json.loads(body)
            pending = data.get('checks', {}).get(
                'telegram_webhook', {}).get('pending_updates', 0)
            if pending and pending != 0:
                self._log(f"Health OK (pending updates: {pending})")
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        # Additional system checks
        alerts = ''

        # Disk usage
        disk_warnings = self._check_disk_usage()
        if disk_warnings:
            alerts += ' '.join(disk_warnings) + ' '

        # Log rotation
        self._rotate_logs()

        # Backup freshness (0=fresh, 1=stale, 2=unmounted)
        backup_rc = self._check_backup_freshness()
        if backup_rc == 2:
            alerts += (
                f"Backup drive not mounted ({self.backup_dest}). ")
        elif backup_rc == 1:
            alerts += 'Backups are stale. '

        # Recent log analysis
        log_issues = self._check_recent_logs()
        if log_issues:
            alerts += '\nLog issues detected:\n' + log_issues

        # Send combined alert if anything needs attention
        if alerts:
            try:
                self._send_alert(
                    f"\u26a0\ufe0f Health check warnings: {alerts}")
            except Exception:
                pass  # _send_alert already logs on failure

    def _handle_connection_refused(self):
        """Server unreachable (connection refused). Attempt restart."""
        self._log_error(
            f"Could not connect to server on port {self.port}")

        fail_count = self._get_fail_count()
        if fail_count >= MAX_RESTART_ATTEMPTS:
            self._log(
                f"Restart skipped (already attempted {fail_count} "
                f"times, manual intervention required)")
            return

        # Throttle restart attempts
        if os.path.isfile(self.restart_stamp):
            last_attempt = self._get_stamp_time()
            now = int(time.time())
            elapsed = now - last_attempt
            if elapsed < MIN_RESTART_INTERVAL:
                self._log(
                    f"Restart skipped (last attempt {elapsed}s ago, "
                    f"throttle: {MIN_RESTART_INTERVAL}s)")
                return

        self._attempt_restart(fail_count)

    def _attempt_restart(self, fail_count):
        """Attempt to restart the service via systemd or launchd."""
        # Check if the service unit/plist exists
        can_restart = False
        if platform.system() == 'Darwin':
            try:
                result = subprocess.run(
                    ['launchctl', 'list'],
                    capture_output=True, text=True, timeout=10)
                if 'com.claudio.server' in result.stdout:
                    can_restart = True
                else:
                    self._log_error(
                        "Service plist not found, cannot auto-restart")
            except (subprocess.TimeoutExpired, OSError):
                self._log_error(
                    "Service plist not found, cannot auto-restart")
        else:
            try:
                result = subprocess.run(
                    ['systemctl', '--user', 'list-unit-files'],
                    capture_output=True, text=True, timeout=10)
                if 'claudio' in result.stdout:
                    can_restart = True
                else:
                    # Distinguish between missing unit and inactive manager
                    unit_path = os.path.join(
                        os.path.expanduser('~'),
                        '.config', 'systemd', 'user',
                        'claudio.service')
                    if os.path.isfile(unit_path):
                        user = os.environ.get(
                            'USER', '') or os.popen(
                            'id -un').read().strip()
                        self._log_error(
                            "User systemd manager not running "
                            "(linger may be disabled). "
                            f"Run: loginctl enable-linger {user}")
                    else:
                        self._log_error(
                            "Service unit not found, "
                            "cannot auto-restart")
            except (subprocess.TimeoutExpired, OSError):
                self._log_error(
                    "Service unit not found, cannot auto-restart")

        if not can_restart:
            return

        # Attempt restart
        self._touch_stamp()
        restart_ok = False

        if platform.system() == 'Darwin':
            subprocess.run(
                ['launchctl', 'stop', 'com.claudio.server'],
                capture_output=True, timeout=10)
            result = subprocess.run(
                ['launchctl', 'start', 'com.claudio.server'],
                capture_output=True, timeout=10)
            restart_ok = (result.returncode == 0)
        else:
            result = subprocess.run(
                ['systemctl', '--user', 'restart', 'claudio'],
                capture_output=True, timeout=30)
            restart_ok = (result.returncode == 0)

        # Track attempt count regardless of restart command outcome
        fail_count += 1
        self._set_fail_count(fail_count)

        if restart_ok:
            self._log(
                f"Service restarted "
                f"(attempt {fail_count}/{MAX_RESTART_ATTEMPTS})")
        else:
            # Remove stamp on failed restart command
            try:
                os.remove(self.restart_stamp)
            except OSError:
                pass
            self._log_error(
                f"Failed to restart service "
                f"(attempt {fail_count}/{MAX_RESTART_ATTEMPTS})")

        if fail_count >= MAX_RESTART_ATTEMPTS:
            self._log_error(
                "Max restart attempts reached, sending alert")
            try:
                self._send_alert(
                    f"\u26a0\ufe0f Claudio server is down after "
                    f"{MAX_RESTART_ATTEMPTS} restart attempts. "
                    f"Please check the server manually.")
            except Exception:
                pass

    def _send_alert(self, message):
        """Send a Telegram alert via direct HTTP call (self-contained)."""
        if not self.telegram_token or not self.telegram_chat_id:
            self._log_error(
                "Cannot send alert: TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID not set")
            return

        url = (f"https://api.telegram.org/"
               f"bot{self.telegram_token}/sendMessage")
        payload = json.dumps({
            'chat_id': self.telegram_chat_id,
            'text': message,
        }).encode('utf-8')

        req = urllib.request.Request(
            url,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            self._log_error(f"Failed to send Telegram alert: {e}")

    # -- Fail state management --

    def _get_fail_count(self):
        """Read current restart attempt count (0 if file missing/invalid)."""
        try:
            with open(self.fail_count_file) as f:
                val = f.read().strip()
            return int(val) if val.isdigit() else 0
        except (OSError, ValueError):
            return 0

    def _set_fail_count(self, n):
        """Write restart attempt count atomically."""
        tmp = self.fail_count_file + '.tmp'
        try:
            with open(tmp, 'w') as f:
                f.write(str(n))
            os.replace(tmp, self.fail_count_file)
        except OSError:
            pass

    def _touch_stamp(self):
        """Record current epoch timestamp in restart stamp file."""
        tmp = self.restart_stamp + '.tmp'
        try:
            with open(tmp, 'w') as f:
                f.write(str(int(time.time())))
            os.replace(tmp, self.restart_stamp)
        except OSError:
            pass

    def _get_stamp_time(self):
        """Read epoch timestamp from restart stamp file (0 if missing)."""
        try:
            with open(self.restart_stamp) as f:
                val = f.read().strip()
            return int(val) if val.isdigit() else 0
        except (OSError, ValueError):
            return 0

    def _clear_fail_state(self):
        """Remove restart stamp and fail count files."""
        for path in (self.restart_stamp, self.fail_count_file):
            try:
                os.remove(path)
            except OSError:
                pass

    # -- Additional system checks --

    def _check_disk_usage(self):
        """Check disk usage of / and backup_dest. Returns list of warnings."""
        warnings = []
        paths = ['/']
        if self.backup_dest and os.path.isdir(self.backup_dest):
            paths.append(self.backup_dest)

        for path in paths:
            try:
                usage = shutil.disk_usage(path)
                percent = int((usage.used / usage.total) * 100)
                if percent >= self.disk_threshold:
                    self._log_warn(
                        f"Disk usage high: {path} at {percent}%")
                    warnings.append(
                        f"Disk usage above {self.disk_threshold}%.")
            except OSError:
                pass
        return warnings

    def _rotate_logs(self):
        """Rotate log files exceeding log_max_size. Returns count rotated."""
        rotated = 0
        pattern = os.path.join(self.claudio_path, '*.log')
        for log_file in glob.glob(pattern):
            try:
                size = os.path.getsize(log_file)
                if size > self.log_max_size:
                    os.rename(log_file, log_file + '.1')
                    self._log(
                        f"Rotated {log_file} ({size} bytes)")
                    rotated += 1
            except OSError:
                pass
        return rotated

    def _check_backup_freshness(self):
        """Check if the most recent backup is within backup_max_age.

        Returns:
            0: fresh (or no backup dest configured)
            1: stale
            2: unmounted
        """
        # Check if backup dest looks like external drive but isn't mounted
        if (self.backup_dest.startswith(('/mnt/', '/media/'))
                and os.path.isdir(self.backup_dest)):
            if not self._check_mount(self.backup_dest):
                self._log_warn(
                    f"Backup destination {self.backup_dest} "
                    f"is not mounted")
                return 2

        backup_dir = os.path.join(
            self.backup_dest, 'claudio-backups', 'hourly')
        if not os.path.isdir(backup_dir):
            return 0  # no backups configured yet

        latest = os.path.join(backup_dir, 'latest')
        if not os.path.islink(latest) and not os.path.isdir(latest):
            # No latest symlink -- find newest directory
            try:
                entries = [
                    e for e in os.listdir(backup_dir)
                    if os.path.isdir(os.path.join(backup_dir, e))
                    and e != 'latest'
                ]
                if entries:
                    entries.sort()
                    latest = os.path.join(backup_dir, entries[-1])
                else:
                    return 1  # backup dir exists but empty
            except OSError:
                return 1

        # Resolve symlink
        if os.path.islink(latest):
            latest = os.path.realpath(latest)

        dir_name = os.path.basename(latest)
        match = _BACKUP_DIR_RE.match(dir_name)
        if not match:
            return 1  # can't parse, assume stale

        # Parse timestamp from directory name (YYYY-MM-DD_HHMM)
        try:
            backup_time = datetime(
                int(match.group(1)), int(match.group(2)),
                int(match.group(3)), int(match.group(4)),
                int(match.group(5)))
            backup_epoch = int(backup_time.timestamp())
        except (ValueError, OSError):
            return 1

        now = int(time.time())
        age = now - backup_epoch
        if age > self.backup_max_age:
            self._log_warn(
                f"Backup stale: last backup {age}s ago "
                f"(threshold: {self.backup_max_age}s)")
            return 1

        return 0

    def _check_mount(self, path):
        """Check if path is on a mounted filesystem (not just /).

        Returns True if mounted, False if the path resolves to /.
        """
        try:
            result = subprocess.run(
                ['findmnt', '--target', path, '-n', '-o', 'TARGET'],
                capture_output=True, text=True, timeout=5)
            target = result.stdout.strip()
            if not target or target == '/':
                return False
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Fallback to mountpoint command
        try:
            mount_root = '/' + '/'.join(
                path.strip('/').split('/')[:2])
            result = subprocess.run(
                ['mountpoint', '-q', mount_root],
                capture_output=True, timeout=5)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Can't determine, assume mounted
        return True

    def _check_recent_logs(self):
        """Scan claudio.log for issues within log_check_window seconds.

        Returns alert text (empty string if nothing found).
        Respects log_alert_cooldown between alerts.
        """
        if not os.path.isfile(self.log_file):
            return ''

        # Throttle: skip if we alerted recently
        if os.path.isfile(self.log_alert_stamp):
            try:
                with open(self.log_alert_stamp) as f:
                    last_alert = int(f.read().strip())
                if int(time.time()) - last_alert < self.log_alert_cooldown:
                    return ''
            except (OSError, ValueError):
                pass

        cutoff = datetime.fromtimestamp(
            time.time() - self.log_check_window)
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')

        # Extract recent lines within time window
        recent_lines = []
        try:
            with open(self.log_file) as f:
                for line in f:
                    match = _LOG_TS_RE.match(line)
                    if match and match.group(1) >= cutoff_str:
                        recent_lines.append(line.rstrip('\n'))
        except OSError:
            return ''

        if not recent_lines:
            return ''

        issues = ''

        # 1. ERROR lines (excluding health-check's own connection errors)
        error_lines = [
            line for line in recent_lines
            if 'ERROR:' in line
            and 'Could not connect to server' not in line
            and 'Cannot send alert' not in line
        ]
        if error_lines:
            # Strip timestamp prefix from sample
            sample = re.sub(r'^\[[^\]]*\] ', '', error_lines[-1])
            issues += (
                f"{len(error_lines)} error(s): `{sample}`\n")

        # 2. Rapid server restarts
        restart_count = sum(
            1 for line in recent_lines
            if 'Starting Claudio server' in line)
        if restart_count >= 3:
            issues += (
                f"Server restarted {restart_count} times "
                f"in {self.log_check_window}s\n")

        # 3. Claude pre-flight warnings
        preflight_count = sum(
            1 for line in recent_lines
            if 'Pre-flight check is taking longer' in line)
        if preflight_count >= 3:
            issues += (
                f"Claude API slow "
                f"({preflight_count} pre-flight warnings)\n")

        # 4. WARN lines (not already covered by disk/backup warnings)
        warn_lines = [
            line for line in recent_lines
            if 'WARN:' in line
            and 'Disk usage' not in line
            and 'Backup stale' not in line
            and 'not mounted' not in line
        ]
        if warn_lines:
            sample = re.sub(r'^\[[^\]]*\] ', '', warn_lines[-1])
            issues += (
                f"{len(warn_lines)} warning(s): `{sample}`\n")

        if issues:
            # Record alert timestamp
            tmp = self.log_alert_stamp + '.tmp'
            try:
                with open(tmp, 'w') as f:
                    f.write(str(int(time.time())))
                os.replace(tmp, self.log_alert_stamp)
            except OSError:
                pass

        return issues

    # -- Logging --

    def _log(self, msg):
        """Write an info log line to claudio.log."""
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{ts}] [health-check] {msg}\n"
        try:
            os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
            with open(self.log_file, 'a') as f:
                f.write(line)
        except OSError:
            pass

    def _log_error(self, msg):
        """Write an error log line to claudio.log."""
        self._log(f"ERROR: {msg}")

    def _log_warn(self, msg):
        """Write a warning log line to claudio.log."""
        self._log(f"WARN: {msg}")


if __name__ == '__main__':
    sys.exit(HealthChecker().run())
