#!/usr/bin/env python3
"""Tests for lib/health_check.py -- health check and monitoring."""

import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.health_check import HealthChecker, _parse_env_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_urlopen_response(body, code=200):
    """Build a MagicMock that behaves like an HTTPResponse from urlopen."""
    if isinstance(body, dict):
        body = json.dumps(body).encode('utf-8')
    elif isinstance(body, str):
        body = body.encode('utf-8')
    resp = MagicMock()
    resp.read.return_value = body
    resp.code = code
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_checker(tmp_path, service_env=None, bot_env=None):
    """Create a HealthChecker with a tmp_path-based claudio directory."""
    claudio_path = str(tmp_path / '.claudio')
    os.makedirs(claudio_path, exist_ok=True)

    # Write service.env
    env_content = 'PORT="8421"\n'
    if service_env:
        for k, v in service_env.items():
            env_content += f'{k}="{v}"\n'
    (tmp_path / '.claudio' / 'service.env').write_text(env_content)

    # Write first bot config if provided
    if bot_env:
        bot_dir = tmp_path / '.claudio' / 'bots' / 'testbot'
        bot_dir.mkdir(parents=True, exist_ok=True)
        bot_content = ''
        for k, v in bot_env.items():
            bot_content += f'{k}="{v}"\n'
        (bot_dir / 'bot.env').write_text(bot_content)

    checker = HealthChecker(claudio_path=claudio_path)
    checker._load_config()
    return checker


# ---------------------------------------------------------------------------
# TestParseEnvFile
# ---------------------------------------------------------------------------

class TestParseEnvFile:
    def test_basic_key_value(self, tmp_path):
        f = tmp_path / 'test.env'
        f.write_text('KEY=value\n')
        assert _parse_env_file(str(f)) == {'KEY': 'value'}

    def test_quoted_value(self, tmp_path):
        f = tmp_path / 'test.env'
        f.write_text('KEY="quoted value"\n')
        assert _parse_env_file(str(f)) == {'KEY': 'quoted value'}

    def test_missing_file(self):
        assert _parse_env_file('/nonexistent/path.env') == {}

    def test_skips_comments_and_blanks(self, tmp_path):
        f = tmp_path / 'test.env'
        f.write_text('# comment\n\nKEY=value\n')
        assert _parse_env_file(str(f)) == {'KEY': 'value'}


# ---------------------------------------------------------------------------
# TestHealthChecker -- main flow
# ---------------------------------------------------------------------------

class TestHealthChecker:
    def test_healthy_clears_fail_state(self, tmp_path):
        checker = _make_checker(tmp_path)
        # Create fail state files
        stamp = os.path.join(checker.claudio_path, '.last_restart_attempt')
        fail_count = os.path.join(checker.claudio_path, '.restart_fail_count')
        with open(stamp, 'w') as f:
            f.write(str(int(time.time())))
        with open(fail_count, 'w') as f:
            f.write('2')

        body = json.dumps({'status': 'ok', 'checks': {}})
        mock_resp = _mock_urlopen_response(body, 200)

        with patch('lib.health_check.urllib.request.urlopen',
                   return_value=mock_resp):
            result = checker.run()

        assert result == 0
        assert not os.path.exists(stamp)
        assert not os.path.exists(fail_count)

    def test_healthy_pending_updates_logged(self, tmp_path):
        checker = _make_checker(tmp_path)
        body = json.dumps({
            'status': 'ok',
            'checks': {
                'telegram_webhook': {'pending_updates': 5}
            }
        })
        mock_resp = _mock_urlopen_response(body, 200)

        with patch('lib.health_check.urllib.request.urlopen',
                   return_value=mock_resp):
            result = checker.run()

        assert result == 0
        log_content = (tmp_path / '.claudio' / 'claudio.log').read_text()
        assert 'pending updates: 5' in log_content

    def test_connection_refused_triggers_restart(self, tmp_path):
        checker = _make_checker(tmp_path)

        def mock_urlopen(req, **kwargs):
            raise urllib.error.URLError('Connection refused')

        mock_systemctl_list = MagicMock()
        mock_systemctl_list.stdout = 'claudio.service enabled'
        mock_restart = MagicMock()
        mock_restart.returncode = 0

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=mock_urlopen), \
             patch('lib.health_check.subprocess.run',
                   side_effect=[mock_systemctl_list, mock_restart]), \
             patch('lib.health_check.platform.system',
                   return_value='Linux'):
            result = checker.run()

        assert result == 1
        assert checker._get_fail_count() == 1
        assert os.path.isfile(checker.restart_stamp)

    def test_restart_throttled(self, tmp_path):
        checker = _make_checker(tmp_path)
        # Set recent restart stamp
        checker._touch_stamp()
        checker._set_fail_count(1)

        def mock_urlopen(req, **kwargs):
            raise urllib.error.URLError('Connection refused')

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=mock_urlopen), \
             patch('lib.health_check.subprocess.run') as mock_run:
            result = checker.run()

        assert result == 1
        # subprocess.run should NOT be called (restart throttled)
        mock_run.assert_not_called()
        # Fail count should remain at 1 (not incremented)
        assert checker._get_fail_count() == 1

    def test_max_restart_attempts_sends_alert(self, tmp_path):
        checker = _make_checker(
            tmp_path,
            bot_env={
                'TELEGRAM_BOT_TOKEN': 'testtoken',
                'TELEGRAM_CHAT_ID': '12345',
            })
        checker._set_fail_count(3)

        def mock_urlopen_health(req, **kwargs):
            # Health endpoint fails
            if 'localhost' in (req.full_url if hasattr(req, 'full_url')
                               else req):
                raise urllib.error.URLError('Connection refused')
            # Alert sending should not be reached
            return _mock_urlopen_response({'ok': True}, 200)

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=mock_urlopen_health), \
             patch('lib.health_check.subprocess.run') as mock_run:
            result = checker.run()

        assert result == 1
        # Restart skipped because max attempts reached
        mock_run.assert_not_called()
        log_content = (tmp_path / '.claudio' / 'claudio.log').read_text()
        assert 'manual intervention required' in log_content

    def test_max_reached_after_restart_sends_alert(self, tmp_path):
        """Alert is sent when fail_count reaches MAX after a restart attempt."""
        checker = _make_checker(
            tmp_path,
            bot_env={
                'TELEGRAM_BOT_TOKEN': 'testtoken',
                'TELEGRAM_CHAT_ID': '12345',
            })
        checker._set_fail_count(2)  # One more attempt will hit max

        def mock_urlopen(req, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else req
            if 'localhost' in url:
                raise urllib.error.URLError('Connection refused')
            # Telegram alert call
            return _mock_urlopen_response({'ok': True}, 200)

        mock_systemctl_list = MagicMock()
        mock_systemctl_list.stdout = 'claudio.service enabled'
        mock_restart = MagicMock()
        mock_restart.returncode = 0

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=mock_urlopen) as mock_url, \
             patch('lib.health_check.subprocess.run',
                   side_effect=[mock_systemctl_list, mock_restart]), \
             patch('lib.health_check.platform.system',
                   return_value='Linux'):
            result = checker.run()

        assert result == 1
        assert checker._get_fail_count() == 3
        # Verify Telegram alert was sent (second urlopen call)
        alert_calls = [
            c for c in mock_url.call_args_list
            if hasattr(c[0][0], 'full_url')
            and 'telegram' in c[0][0].full_url
        ]
        assert len(alert_calls) == 1

    def test_unhealthy_503_logs_error(self, tmp_path):
        checker = _make_checker(tmp_path)
        err = urllib.error.HTTPError(
            url='http://localhost:8421/health',
            code=503, msg='Service Unavailable',
            hdrs={},
            fp=io.BytesIO(b'{"status":"unhealthy"}'))

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=err):
            result = checker.run()

        assert result == 1
        log_content = (tmp_path / '.claudio' / 'claudio.log').read_text()
        assert 'Health check returned unhealthy' in log_content

    def test_unexpected_response_logs_error(self, tmp_path):
        checker = _make_checker(tmp_path)
        err = urllib.error.HTTPError(
            url='http://localhost:8421/health',
            code=502, msg='Bad Gateway',
            hdrs={},
            fp=io.BytesIO(b'bad gateway'))

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=err):
            result = checker.run()

        assert result == 1
        log_content = (tmp_path / '.claudio' / 'claudio.log').read_text()
        assert 'Unexpected response (HTTP 502)' in log_content

    def test_missing_env_file_returns_1(self, tmp_path):
        checker = HealthChecker(
            claudio_path=str(tmp_path / 'nonexistent'))
        result = checker.run()
        assert result == 1


# ---------------------------------------------------------------------------
# TestDiskUsage
# ---------------------------------------------------------------------------

class TestDiskUsage:
    def test_below_threshold_ok(self, tmp_path):
        checker = _make_checker(tmp_path)
        # 50% usage
        mock_usage = MagicMock()
        mock_usage.total = 100 * 1024 * 1024 * 1024
        mock_usage.used = 50 * 1024 * 1024 * 1024
        mock_usage.free = 50 * 1024 * 1024 * 1024

        with patch('shutil.disk_usage', return_value=mock_usage):
            warnings = checker._check_disk_usage()

        assert warnings == []

    def test_above_threshold_warns(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.disk_threshold = 80
        # 95% usage
        mock_usage = MagicMock()
        mock_usage.total = 100 * 1024 * 1024 * 1024
        mock_usage.used = 95 * 1024 * 1024 * 1024
        mock_usage.free = 5 * 1024 * 1024 * 1024

        with patch('shutil.disk_usage', return_value=mock_usage):
            warnings = checker._check_disk_usage()

        assert len(warnings) > 0
        assert '80%' in warnings[0]

    def test_oserror_ignored(self, tmp_path):
        checker = _make_checker(tmp_path)

        with patch('shutil.disk_usage', side_effect=OSError('nope')):
            warnings = checker._check_disk_usage()

        assert warnings == []


# ---------------------------------------------------------------------------
# TestLogRotation
# ---------------------------------------------------------------------------

class TestLogRotation:
    def test_rotates_large_log(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_max_size = 100  # 100 bytes

        log_file = tmp_path / '.claudio' / 'test.log'
        log_file.write_text('x' * 200)

        rotated = checker._rotate_logs()

        assert rotated == 1
        assert not log_file.exists()
        assert (tmp_path / '.claudio' / 'test.log.1').exists()

    def test_skips_small_log(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_max_size = 1000

        log_file = tmp_path / '.claudio' / 'test.log'
        log_file.write_text('small log')

        rotated = checker._rotate_logs()

        assert rotated == 0
        assert log_file.exists()
        assert not (tmp_path / '.claudio' / 'test.log.1').exists()

    def test_rotates_multiple_logs(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_max_size = 50

        (tmp_path / '.claudio' / 'a.log').write_text('x' * 100)
        (tmp_path / '.claudio' / 'b.log').write_text('x' * 100)
        (tmp_path / '.claudio' / 'c.log').write_text('small')

        rotated = checker._rotate_logs()

        assert rotated == 2


# ---------------------------------------------------------------------------
# TestBackupFreshness
# ---------------------------------------------------------------------------

class TestBackupFreshness:
    def test_fresh_backup_ok(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.backup_dest = str(tmp_path / 'backups')
        checker.backup_max_age = 7200

        # Create a fresh backup directory
        now = time.time()
        dt = time.strftime('%Y-%m-%d_%H%M', time.localtime(now))
        backup_dir = (tmp_path / 'backups' / 'claudio-backups'
                      / 'hourly' / dt)
        backup_dir.mkdir(parents=True)

        result = checker._check_backup_freshness()
        assert result == 0

    def test_stale_backup_warns(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.backup_dest = str(tmp_path / 'backups')
        checker.backup_max_age = 3600  # 1 hour

        # Create a backup from 2 hours ago
        old_time = time.time() - 7200
        dt = time.strftime('%Y-%m-%d_%H%M', time.localtime(old_time))
        backup_dir = (tmp_path / 'backups' / 'claudio-backups'
                      / 'hourly' / dt)
        backup_dir.mkdir(parents=True)

        result = checker._check_backup_freshness()
        assert result == 1

    def test_no_backup_dir_ok(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.backup_dest = str(tmp_path / 'backups')

        # No claudio-backups/hourly directory exists
        result = checker._check_backup_freshness()
        assert result == 0

    def test_unmounted_drive(self, tmp_path):
        checker = _make_checker(tmp_path)
        # Use /mnt/ prefix to trigger mount check
        checker.backup_dest = '/mnt/ssd'

        with patch.object(checker, '_check_mount', return_value=False), \
             patch('os.path.isdir', return_value=True):
            result = checker._check_backup_freshness()

        assert result == 2

    def test_latest_symlink_resolved(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.backup_dest = str(tmp_path / 'backups')
        checker.backup_max_age = 7200

        now = time.time()
        dt = time.strftime('%Y-%m-%d_%H%M', time.localtime(now))
        hourly_dir = (tmp_path / 'backups' / 'claudio-backups'
                      / 'hourly')
        backup_dir = hourly_dir / dt
        backup_dir.mkdir(parents=True)
        latest = hourly_dir / 'latest'
        latest.symlink_to(backup_dir)

        result = checker._check_backup_freshness()
        assert result == 0

    def test_empty_backup_dir_stale(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.backup_dest = str(tmp_path / 'backups')

        hourly_dir = (tmp_path / 'backups' / 'claudio-backups'
                      / 'hourly')
        hourly_dir.mkdir(parents=True)

        result = checker._check_backup_freshness()
        assert result == 1


# ---------------------------------------------------------------------------
# TestRecentLogs
# ---------------------------------------------------------------------------

class TestRecentLogs:
    def _write_log(self, checker, lines, offset_seconds=0):
        """Write log lines with timestamps relative to now."""
        content = ''
        for line_text in lines:
            ts = time.time() - offset_seconds
            ts_str = time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(ts))
            content += f"[{ts_str}] {line_text}\n"
        os.makedirs(os.path.dirname(checker.log_file), exist_ok=True)
        with open(checker.log_file, 'w') as f:
            f.write(content)

    def test_detects_errors(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_check_window = 300
        self._write_log(checker, [
            '[server] ERROR: Something broke',
            '[server] Processing webhook ok',
        ])

        issues = checker._check_recent_logs()

        assert '1 error(s)' in issues
        assert 'Something broke' in issues

    def test_detects_rapid_restarts(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_check_window = 300
        self._write_log(checker, [
            '[server] Starting Claudio server on port 8421',
            '[server] Starting Claudio server on port 8421',
            '[server] Starting Claudio server on port 8421',
        ])

        issues = checker._check_recent_logs()

        assert 'restarted 3 times' in issues

    def test_respects_cooldown(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_check_window = 300
        checker.log_alert_cooldown = 1800

        # Set a recent alert timestamp
        with open(checker.log_alert_stamp, 'w') as f:
            f.write(str(int(time.time())))

        self._write_log(checker, [
            '[server] ERROR: Something broke',
        ])

        issues = checker._check_recent_logs()
        assert issues == ''

    def test_ignores_old_entries(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_check_window = 60  # 1 minute window

        # Write entries from 5 minutes ago
        self._write_log(checker, [
            '[server] ERROR: Old error',
        ], offset_seconds=300)

        issues = checker._check_recent_logs()
        assert issues == ''

    def test_ignores_health_check_errors(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_check_window = 300
        self._write_log(checker, [
            '[health-check] ERROR: Could not connect to server on port 8421',
            '[health-check] ERROR: Cannot send alert: TELEGRAM_BOT_TOKEN not set',
        ])

        issues = checker._check_recent_logs()
        assert issues == ''

    def test_detects_preflight_warnings(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_check_window = 300
        self._write_log(checker, [
            '[claude] Pre-flight check is taking longer than expected',
            '[claude] Pre-flight check is taking longer than expected',
            '[claude] Pre-flight check is taking longer than expected',
        ])

        issues = checker._check_recent_logs()
        assert 'Claude API slow' in issues
        assert '3 pre-flight warnings' in issues

    def test_detects_warn_lines(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_check_window = 300
        self._write_log(checker, [
            '[server] WARN: Queue depth is growing',
        ])

        issues = checker._check_recent_logs()
        assert '1 warning(s)' in issues
        assert 'Queue depth' in issues

    def test_excludes_disk_backup_warns(self, tmp_path):
        """WARN lines about disk/backup are excluded (handled separately)."""
        checker = _make_checker(tmp_path)
        checker.log_check_window = 300
        self._write_log(checker, [
            '[health-check] WARN: Disk usage high: / at 92%',
            '[health-check] WARN: Backup stale: last backup 8000s ago',
            '[health-check] WARN: /mnt/ssd is not mounted',
        ])

        issues = checker._check_recent_logs()
        assert issues == ''

    def test_updates_alert_stamp(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker.log_check_window = 300
        self._write_log(checker, [
            '[server] ERROR: Something broke',
        ])

        before = time.time()
        checker._check_recent_logs()

        assert os.path.isfile(checker.log_alert_stamp)
        with open(checker.log_alert_stamp) as f:
            stamp_time = int(f.read().strip())
        assert stamp_time >= int(before)


# ---------------------------------------------------------------------------
# TestSendAlert
# ---------------------------------------------------------------------------

class TestSendAlert:
    def test_sends_telegram_message(self, tmp_path):
        checker = _make_checker(
            tmp_path,
            bot_env={
                'TELEGRAM_BOT_TOKEN': 'testtoken123',
                'TELEGRAM_CHAT_ID': '99999',
            })

        mock_resp = _mock_urlopen_response({'ok': True}, 200)

        with patch('lib.health_check.urllib.request.urlopen',
                   return_value=mock_resp) as mock_url:
            checker._send_alert('Test alert message')

        assert mock_url.called
        call_args = mock_url.call_args
        req = call_args[0][0]
        assert 'testtoken123' in req.full_url
        body = json.loads(req.data)
        assert body['chat_id'] == '99999'
        assert body['text'] == 'Test alert message'

    def test_skips_when_no_credentials(self, tmp_path):
        checker = _make_checker(tmp_path)
        # No bot env, so no credentials

        with patch('lib.health_check.urllib.request.urlopen') as mock_url:
            checker._send_alert('Test alert')

        mock_url.assert_not_called()

    def test_logs_on_failure(self, tmp_path):
        checker = _make_checker(
            tmp_path,
            bot_env={
                'TELEGRAM_BOT_TOKEN': 'testtoken123',
                'TELEGRAM_CHAT_ID': '99999',
            })

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=urllib.error.URLError('timeout')):
            checker._send_alert('Test alert')

        log_content = (tmp_path / '.claudio' / 'claudio.log').read_text()
        assert 'Failed to send Telegram alert' in log_content


# ---------------------------------------------------------------------------
# TestFailCount
# ---------------------------------------------------------------------------

class TestFailCount:
    def test_get_set_roundtrip(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker._set_fail_count(5)
        assert checker._get_fail_count() == 5

    def test_get_default_zero(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._get_fail_count() == 0

    def test_invalid_content_returns_zero(self, tmp_path):
        checker = _make_checker(tmp_path)
        with open(checker.fail_count_file, 'w') as f:
            f.write('not-a-number')
        assert checker._get_fail_count() == 0

    def test_clear_fail_state(self, tmp_path):
        checker = _make_checker(tmp_path)
        checker._set_fail_count(3)
        checker._touch_stamp()

        assert os.path.isfile(checker.fail_count_file)
        assert os.path.isfile(checker.restart_stamp)

        checker._clear_fail_state()

        assert not os.path.isfile(checker.fail_count_file)
        assert not os.path.isfile(checker.restart_stamp)


# ---------------------------------------------------------------------------
# TestStamp
# ---------------------------------------------------------------------------

class TestStamp:
    def test_touch_and_get_roundtrip(self, tmp_path):
        checker = _make_checker(tmp_path)
        before = int(time.time())
        checker._touch_stamp()
        after = int(time.time())

        stamp_time = checker._get_stamp_time()
        assert before <= stamp_time <= after

    def test_get_default_zero(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._get_stamp_time() == 0


# ---------------------------------------------------------------------------
# TestCheckMount
# ---------------------------------------------------------------------------

class TestCheckMount:
    def test_findmnt_mounted(self, tmp_path):
        checker = _make_checker(tmp_path)
        mock_result = MagicMock()
        mock_result.stdout = '/mnt/ssd\n'

        with patch('lib.health_check.subprocess.run',
                   return_value=mock_result):
            assert checker._check_mount('/mnt/ssd') is True

    def test_findmnt_root_means_unmounted(self, tmp_path):
        checker = _make_checker(tmp_path)
        mock_result = MagicMock()
        mock_result.stdout = '/\n'

        with patch('lib.health_check.subprocess.run',
                   return_value=mock_result):
            assert checker._check_mount('/mnt/ssd') is False

    def test_findmnt_missing_falls_back_to_mountpoint(self, tmp_path):
        checker = _make_checker(tmp_path)

        mock_mountpoint = MagicMock()
        mock_mountpoint.returncode = 0

        def side_effect(cmd, **kwargs):
            if cmd[0] == 'findmnt':
                raise FileNotFoundError('findmnt not found')
            return mock_mountpoint

        with patch('lib.health_check.subprocess.run',
                   side_effect=side_effect):
            assert checker._check_mount('/mnt/ssd') is True


# ---------------------------------------------------------------------------
# TestLoadConfig
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_loads_service_env(self, tmp_path):
        checker = _make_checker(
            tmp_path,
            service_env={
                'DISK_USAGE_THRESHOLD': '85',
                'LOG_MAX_SIZE': '5242880',
                'BACKUP_MAX_AGE': '3600',
                'BACKUP_DEST': '/mnt/usb',
                'LOG_CHECK_WINDOW': '120',
                'LOG_ALERT_COOLDOWN': '900',
            })

        assert checker.disk_threshold == 85
        assert checker.log_max_size == 5242880
        assert checker.backup_max_age == 3600
        assert checker.backup_dest == '/mnt/usb'
        assert checker.log_check_window == 120
        assert checker.log_alert_cooldown == 900

    def test_loads_bot_credentials(self, tmp_path):
        checker = _make_checker(
            tmp_path,
            bot_env={
                'TELEGRAM_BOT_TOKEN': 'abc123',
                'TELEGRAM_CHAT_ID': '456',
            })

        assert checker.telegram_token == 'abc123'
        assert checker.telegram_chat_id == '456'

    def test_defaults_when_no_bot(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker.telegram_token == ''
        assert checker.telegram_chat_id == ''


# ---------------------------------------------------------------------------
# TestRestartFlow (integration-style)
# ---------------------------------------------------------------------------

class TestRestartFlow:
    def test_restart_on_darwin(self, tmp_path):
        checker = _make_checker(tmp_path)

        def mock_urlopen(req, **kwargs):
            raise urllib.error.URLError('Connection refused')

        mock_launchctl_list = MagicMock()
        mock_launchctl_list.stdout = 'com.claudio.server\t0\tcom.claudio.server'
        mock_stop = MagicMock()
        mock_stop.returncode = 0
        mock_start = MagicMock()
        mock_start.returncode = 0

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=mock_urlopen), \
             patch('lib.health_check.subprocess.run',
                   side_effect=[mock_launchctl_list, mock_stop, mock_start]), \
             patch('lib.health_check.platform.system',
                   return_value='Darwin'):
            result = checker.run()

        assert result == 1
        assert checker._get_fail_count() == 1
        log_content = (tmp_path / '.claudio' / 'claudio.log').read_text()
        assert 'Service restarted' in log_content

    def test_failed_restart_removes_stamp(self, tmp_path):
        checker = _make_checker(tmp_path)

        def mock_urlopen(req, **kwargs):
            raise urllib.error.URLError('Connection refused')

        mock_systemctl_list = MagicMock()
        mock_systemctl_list.stdout = 'claudio.service enabled'
        mock_restart = MagicMock()
        mock_restart.returncode = 1  # restart failed

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=mock_urlopen), \
             patch('lib.health_check.subprocess.run',
                   side_effect=[mock_systemctl_list, mock_restart]), \
             patch('lib.health_check.platform.system',
                   return_value='Linux'):
            result = checker.run()

        assert result == 1
        assert checker._get_fail_count() == 1
        # Stamp should be removed on failed restart
        assert not os.path.isfile(checker.restart_stamp)
        log_content = (tmp_path / '.claudio' / 'claudio.log').read_text()
        assert 'Failed to restart service' in log_content

    def test_no_service_unit_skips_restart(self, tmp_path):
        checker = _make_checker(tmp_path)

        def mock_urlopen(req, **kwargs):
            raise urllib.error.URLError('Connection refused')

        mock_systemctl_list = MagicMock()
        mock_systemctl_list.stdout = ''  # no claudio unit

        # Must also mock os.path.isfile for the unit path check fallback
        real_isfile = os.path.isfile

        def fake_isfile(path):
            if 'claudio.service' in str(path):
                return False
            return real_isfile(path)

        with patch('lib.health_check.urllib.request.urlopen',
                   side_effect=mock_urlopen), \
             patch('lib.health_check.subprocess.run',
                   return_value=mock_systemctl_list), \
             patch('lib.health_check.platform.system',
                   return_value='Linux'), \
             patch('os.path.isfile', side_effect=fake_isfile):
            result = checker.run()

        assert result == 1
        assert checker._get_fail_count() == 0
        log_content = (tmp_path / '.claudio' / 'claudio.log').read_text()
        assert 'Service unit not found' in log_content
