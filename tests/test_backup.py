#!/usr/bin/env python3
"""Tests for lib/backup.py -- backup management."""

import os
import sys
from unittest.mock import MagicMock, patch


# Add parent dir to path so we can import lib/backup.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lib.backup as backup


# -- _check_mount --


class TestCheckMount:
    def test_non_external_path_always_mounted(self):
        """Paths not under /mnt or /media skip mount checks entirely."""
        assert backup._check_mount('/home/user/backups') is True
        assert backup._check_mount('/tmp/backups') is True
        assert backup._check_mount('/var/data') is True

    @patch('lib.backup.subprocess.run')
    def test_mounted_external_path(self, mock_run):
        mock_run.return_value = MagicMock(stdout='/mnt/ssd\n', returncode=0)
        assert backup._check_mount('/mnt/ssd/backups') is True
        mock_run.assert_called_once_with(
            ['findmnt', '--target', '/mnt/ssd/backups', '-n', '-o', 'TARGET'],
            capture_output=True, text=True, timeout=10,
        )

    @patch('lib.backup.subprocess.run')
    def test_unmounted_external_path_root_target(self, mock_run):
        """If findmnt reports '/' as mount target, drive is not mounted."""
        mock_run.return_value = MagicMock(stdout='/\n', returncode=0)
        assert backup._check_mount('/mnt/ssd/backups') is False

    @patch('lib.backup.subprocess.run')
    def test_unmounted_external_path_empty_target(self, mock_run):
        """If findmnt returns empty output, drive is not mounted."""
        mock_run.return_value = MagicMock(stdout='', returncode=1)
        assert backup._check_mount('/mnt/ssd/backups') is False

    @patch('lib.backup.subprocess.run')
    def test_findmnt_not_found_falls_back_to_mountpoint(self, mock_run):
        """When findmnt is missing, falls back to mountpoint command."""
        def side_effect(cmd, **kwargs):
            if cmd[0] == 'findmnt':
                raise FileNotFoundError
            # mountpoint -q returns 0 for mounted
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        assert backup._check_mount('/media/usb/data') is True

    @patch('lib.backup.subprocess.run')
    def test_mountpoint_not_mounted(self, mock_run):
        """When mountpoint reports not mounted."""
        def side_effect(cmd, **kwargs):
            if cmd[0] == 'findmnt':
                raise FileNotFoundError
            # mountpoint -q returns 1 for not mounted
            return MagicMock(returncode=1)

        mock_run.side_effect = side_effect
        assert backup._check_mount('/mnt/ssd/data') is False

    @patch('lib.backup.subprocess.run')
    def test_media_path_checked(self, mock_run):
        """Paths under /media/ are also checked."""
        mock_run.return_value = MagicMock(stdout='/media/pi/usbdrive\n', returncode=0)
        assert backup._check_mount('/media/pi/usbdrive/backup') is True


# -- _safe_dest_path --


class TestSafeDestPath:
    def test_valid_simple_path(self):
        assert backup._safe_dest_path('/mnt/ssd/backups') is True

    def test_valid_path_with_dots_underscores(self):
        assert backup._safe_dest_path('/mnt/ssd/my_backups/v1.0') is True

    def test_rejects_spaces(self):
        assert backup._safe_dest_path('/mnt/ssd/my backups') is False

    def test_rejects_semicolons(self):
        assert backup._safe_dest_path('/mnt/ssd;rm -rf /') is False

    def test_rejects_backticks(self):
        assert backup._safe_dest_path('/mnt/ssd/`whoami`') is False

    def test_rejects_dollar(self):
        assert backup._safe_dest_path('/mnt/$HOME/backups') is False

    def test_rejects_ampersand(self):
        assert backup._safe_dest_path('/mnt/ssd&') is False

    def test_rejects_newline(self):
        assert backup._safe_dest_path('/mnt/ssd\n/evil') is False

    def test_empty_string(self):
        assert backup._safe_dest_path('') is False


# -- _backup_rotate --


class TestBackupRotate:
    def test_keeps_newest_when_over_limit(self, tmp_path):
        """When count > keep, delete the oldest (sorted lexicographically)."""
        d = tmp_path / "hourly"
        d.mkdir()
        names = ['2025-01-01_0000', '2025-01-01_0100', '2025-01-01_0200',
                 '2025-01-01_0300', '2025-01-01_0400']
        for name in names:
            (d / name).mkdir()

        backup._backup_rotate(str(d), 3)

        remaining = sorted(os.listdir(str(d)))
        assert remaining == ['2025-01-01_0200', '2025-01-01_0300', '2025-01-01_0400']

    def test_no_deletion_when_under_limit(self, tmp_path):
        d = tmp_path / "hourly"
        d.mkdir()
        names = ['2025-01-01_0000', '2025-01-01_0100']
        for name in names:
            (d / name).mkdir()

        backup._backup_rotate(str(d), 5)

        remaining = sorted(os.listdir(str(d)))
        assert remaining == names

    def test_exact_limit_no_deletion(self, tmp_path):
        d = tmp_path / "hourly"
        d.mkdir()
        names = ['2025-01-01_0000', '2025-01-01_0100', '2025-01-01_0200']
        for name in names:
            (d / name).mkdir()

        backup._backup_rotate(str(d), 3)

        remaining = sorted(os.listdir(str(d)))
        assert remaining == names

    def test_ignores_symlinks(self, tmp_path):
        """Symlinks (like 'latest') should not be counted or deleted."""
        d = tmp_path / "hourly"
        d.mkdir()
        names = ['2025-01-01_0000', '2025-01-01_0100']
        for name in names:
            (d / name).mkdir()
        # Create a symlink that should be ignored
        os.symlink(str(d / '2025-01-01_0100'), str(d / 'latest'))

        backup._backup_rotate(str(d), 2)

        remaining = sorted(os.listdir(str(d)))
        assert 'latest' in remaining
        assert '2025-01-01_0000' in remaining
        assert '2025-01-01_0100' in remaining

    def test_ignores_files(self, tmp_path):
        """Regular files should not be counted or deleted."""
        d = tmp_path / "hourly"
        d.mkdir()
        names = ['2025-01-01_0000', '2025-01-01_0100', '2025-01-01_0200']
        for name in names:
            (d / name).mkdir()
        (d / 'README.txt').write_text('info')

        backup._backup_rotate(str(d), 2)

        remaining = sorted(os.listdir(str(d)))
        assert 'README.txt' in remaining
        # Kept the 2 newest directories
        assert '2025-01-01_0100' in remaining
        assert '2025-01-01_0200' in remaining
        assert '2025-01-01_0000' not in remaining

    def test_nonexistent_directory(self, tmp_path):
        """Should not raise for a nonexistent directory."""
        backup._backup_rotate(str(tmp_path / 'nope'), 5)

    def test_empty_directory(self, tmp_path):
        d = tmp_path / "hourly"
        d.mkdir()
        backup._backup_rotate(str(d), 3)
        assert os.listdir(str(d)) == []


# -- backup_run --


class TestBackupRun:
    @patch('lib.backup._check_mount', return_value=True)
    @patch('lib.backup.subprocess.run')
    def test_successful_backup(self, mock_run, mock_mount, tmp_path):
        dest = str(tmp_path / 'dest')
        os.makedirs(dest)
        src = str(tmp_path / 'claudio_home')
        os.makedirs(src)

        # rsync succeeds, cp -al succeeds
        mock_run.return_value = MagicMock(returncode=0, stderr='', stdout='')

        result = backup.backup_run(dest, claudio_path=src)
        assert result == 0

        # Verify directory structure was created
        backup_root = os.path.join(dest, 'claudio-backups')
        assert os.path.isdir(os.path.join(backup_root, 'hourly'))
        assert os.path.isdir(os.path.join(backup_root, 'daily'))

    @patch('lib.backup._check_mount', return_value=True)
    @patch('lib.backup.subprocess.run')
    def test_rsync_called_with_correct_args(self, mock_run, mock_mount, tmp_path):
        dest = str(tmp_path / 'dest')
        os.makedirs(dest)
        src = str(tmp_path / 'src')
        os.makedirs(src)

        mock_run.return_value = MagicMock(returncode=0, stderr='', stdout='')

        backup.backup_run(dest, claudio_path=src)

        # First call should be rsync
        first_call = mock_run.call_args_list[0]
        args = first_call[0][0]
        assert args[0] == 'rsync'
        assert '-a' in args
        assert '--delete' in args
        assert args[-2] == src + '/'
        assert 'hourly' in args[-1]

    @patch('lib.backup._check_mount', return_value=True)
    @patch('lib.backup.subprocess.run')
    def test_rsync_uses_link_dest_when_latest_exists(self, mock_run, mock_mount, tmp_path):
        dest = str(tmp_path / 'dest')
        os.makedirs(dest)
        src = str(tmp_path / 'src')
        os.makedirs(src)

        # Create a 'latest' symlink pointing to an existing directory
        hourly_dir = os.path.join(dest, 'claudio-backups', 'hourly')
        os.makedirs(hourly_dir)
        prev = os.path.join(hourly_dir, '2025-01-01_0000')
        os.makedirs(prev)
        os.symlink(prev, os.path.join(hourly_dir, 'latest'))

        mock_run.return_value = MagicMock(returncode=0, stderr='', stdout='')

        backup.backup_run(dest, claudio_path=src)

        first_call = mock_run.call_args_list[0]
        args = first_call[0][0]
        assert '--link-dest' in args

    def test_empty_dest_returns_error(self, capsys):
        result = backup.backup_run('', claudio_path='/tmp')
        assert result == 1
        captured = capsys.readouterr()
        assert 'required' in captured.err

    def test_nonexistent_dest_returns_error(self, capsys):
        result = backup.backup_run('/nonexistent/path', claudio_path='/tmp')
        assert result == 1
        captured = capsys.readouterr()
        assert 'does not exist' in captured.err

    @patch('lib.backup._check_mount', return_value=False)
    def test_unmounted_dest_returns_error(self, mock_mount, tmp_path, capsys):
        dest = str(tmp_path / 'dest')
        os.makedirs(dest)
        result = backup.backup_run(dest, claudio_path='/tmp')
        assert result == 1
        captured = capsys.readouterr()
        assert 'mounted' in captured.err

    @patch('lib.backup._check_mount', return_value=True)
    @patch('lib.backup.subprocess.run')
    def test_rsync_failure_returns_error(self, mock_run, mock_mount, tmp_path, capsys):
        dest = str(tmp_path / 'dest')
        os.makedirs(dest)
        src = str(tmp_path / 'src')
        os.makedirs(src)

        mock_run.return_value = MagicMock(returncode=1, stderr='rsync error', stdout='')

        result = backup.backup_run(dest, claudio_path=src)
        assert result == 1
        captured = capsys.readouterr()
        assert 'rsync failed' in captured.err

    @patch('lib.backup._check_mount', return_value=True)
    @patch('lib.backup.subprocess.run')
    def test_latest_symlink_updated(self, mock_run, mock_mount, tmp_path):
        dest = str(tmp_path / 'dest')
        os.makedirs(dest)
        src = str(tmp_path / 'src')
        os.makedirs(src)

        mock_run.return_value = MagicMock(returncode=0, stderr='', stdout='')

        backup.backup_run(dest, claudio_path=src)

        latest = os.path.join(dest, 'claudio-backups', 'hourly', 'latest')
        assert os.path.islink(latest)

    @patch('lib.backup._check_mount', return_value=True)
    @patch('lib.backup.subprocess.run')
    def test_daily_promotion_via_cp_al(self, mock_run, mock_mount, tmp_path):
        """When cp -al succeeds, no rsync fallback is needed for daily."""
        dest = str(tmp_path / 'dest')
        os.makedirs(dest)
        src = str(tmp_path / 'src')
        os.makedirs(src)

        # rsync is mocked, so it won't create the hourly dir on disk.
        # Simulate rsync creating the snapshot directory as a side effect.
        def run_side_effect(cmd, **kwargs):
            if cmd[0] == 'rsync':
                # Create the target directory that rsync would create
                os.makedirs(cmd[-1].rstrip('/'), exist_ok=True)
            return MagicMock(returncode=0, stderr='', stdout='')

        mock_run.side_effect = run_side_effect

        backup.backup_run(dest, claudio_path=src)

        # Should have called rsync (hourly) and cp -al (daily promotion)
        commands_called = [c[0][0][0] for c in mock_run.call_args_list]
        assert 'rsync' in commands_called
        assert 'cp' in commands_called

    @patch('lib.backup._check_mount', return_value=True)
    @patch('lib.backup.subprocess.run')
    def test_daily_promotion_falls_back_to_rsync(self, mock_run, mock_mount, tmp_path):
        """When cp -al fails, rsync --link-dest is used for daily promotion."""
        dest = str(tmp_path / 'dest')
        os.makedirs(dest)
        src = str(tmp_path / 'src')
        os.makedirs(src)

        def run_side_effect(cmd, **kwargs):
            if cmd[0] == 'rsync':
                # Simulate rsync creating the target directory
                os.makedirs(cmd[-1].rstrip('/'), exist_ok=True)
                return MagicMock(returncode=0, stderr='', stdout='')
            if cmd[0] == 'cp':
                return MagicMock(returncode=1, stderr='cp: unsupported', stdout='')
            return MagicMock(returncode=0, stderr='', stdout='')

        mock_run.side_effect = run_side_effect

        result = backup.backup_run(dest, claudio_path=src)
        assert result == 0

        # Should have 3 subprocess calls: rsync (hourly), cp -al (fail), rsync (daily)
        commands_called = [c[0][0][0] for c in mock_run.call_args_list]
        assert commands_called.count('rsync') == 2
        assert commands_called.count('cp') == 1

    @patch('lib.backup._check_mount', return_value=True)
    @patch('lib.backup.subprocess.run')
    def test_rotation_called(self, mock_run, mock_mount, tmp_path):
        """Verify rotation removes old snapshots when over limit."""
        dest = str(tmp_path / 'dest')
        os.makedirs(dest)
        src = str(tmp_path / 'src')
        os.makedirs(src)

        # Pre-create some old hourly directories
        hourly_dir = os.path.join(dest, 'claudio-backups', 'hourly')
        os.makedirs(hourly_dir)
        for i in range(5):
            os.makedirs(os.path.join(hourly_dir, f'2025-01-01_000{i}'))

        mock_run.return_value = MagicMock(returncode=0, stderr='', stdout='')

        backup.backup_run(dest, max_hourly=3, claudio_path=src)

        # After rotation, should have at most 3 hourly snapshots (dirs, not symlinks)
        entries = [
            e for e in os.listdir(hourly_dir)
            if os.path.isdir(os.path.join(hourly_dir, e))
            and not os.path.islink(os.path.join(hourly_dir, e))
        ]
        assert len(entries) <= 3


# -- backup_status --


class TestBackupStatus:
    def test_no_backups_found(self, tmp_path, capsys):
        result = backup.backup_status(str(tmp_path))
        assert result == 0
        captured = capsys.readouterr()
        assert 'No backups found' in captured.out

    def test_empty_dest_returns_error(self, capsys):
        result = backup.backup_status('')
        assert result == 1
        captured = capsys.readouterr()
        assert 'required' in captured.err

    @patch('lib.backup.subprocess.run')
    def test_shows_snapshot_counts(self, mock_run, tmp_path, capsys):
        backup_root = tmp_path / 'claudio-backups'
        hourly = backup_root / 'hourly'
        daily = backup_root / 'daily'
        hourly.mkdir(parents=True)
        daily.mkdir(parents=True)

        (hourly / '2025-01-01_0000').mkdir()
        (hourly / '2025-01-01_0100').mkdir()
        (hourly / '2025-01-01_0200').mkdir()
        (daily / '2025-01-01').mkdir()

        mock_run.return_value = MagicMock(returncode=0, stdout='1.2G\t/path\n')

        result = backup.backup_status(str(tmp_path))
        assert result == 0

        captured = capsys.readouterr()
        assert 'Hourly backups: 3' in captured.out
        assert 'Oldest: 2025-01-01_0000' in captured.out
        assert 'Newest: 2025-01-01_0200' in captured.out
        assert 'Daily backups: 1' in captured.out
        assert 'Oldest: 2025-01-01' in captured.out
        assert 'Total size: 1.2G' in captured.out

    @patch('lib.backup.subprocess.run')
    def test_excludes_symlinks_from_count(self, mock_run, tmp_path, capsys):
        backup_root = tmp_path / 'claudio-backups'
        hourly = backup_root / 'hourly'
        hourly.mkdir(parents=True)
        (backup_root / 'daily').mkdir()

        (hourly / '2025-01-01_0000').mkdir()
        (hourly / '2025-01-01_0100').mkdir()
        os.symlink(str(hourly / '2025-01-01_0100'), str(hourly / 'latest'))

        mock_run.return_value = MagicMock(returncode=0, stdout='500M\t/path\n')

        backup.backup_status(str(tmp_path))

        captured = capsys.readouterr()
        assert 'Hourly backups: 2' in captured.out

    @patch('lib.backup.subprocess.run')
    def test_du_failure_shows_unknown(self, mock_run, tmp_path, capsys):
        backup_root = tmp_path / 'claudio-backups'
        (backup_root / 'hourly').mkdir(parents=True)
        (backup_root / 'daily').mkdir(parents=True)

        mock_run.return_value = MagicMock(returncode=1, stdout='')

        backup.backup_status(str(tmp_path))

        captured = capsys.readouterr()
        assert 'Total size: unknown' in captured.out


# -- backup_cron_install --


class TestBackupCronInstall:
    @patch('lib.backup._write_crontab')
    @patch('lib.backup._read_crontab', return_value=[])
    def test_installs_cron_entry(self, mock_read, mock_write, tmp_path, capsys):
        dest = str(tmp_path)
        claudio_bin = '/usr/local/bin/claudio'
        claudio_path = str(tmp_path / '.claudio')

        result = backup.backup_cron_install(
            dest, max_hourly=12, max_daily=5,
            claudio_bin=claudio_bin, claudio_path=claudio_path,
        )
        assert result == 0

        written_lines = mock_write.call_args[0][0]
        assert len(written_lines) == 1
        entry = written_lines[0]
        assert entry.startswith('0 * * * *')
        assert claudio_bin in entry
        assert dest in entry
        assert '--hours 12' in entry
        assert '--days 5' in entry
        assert backup.BACKUP_CRON_MARKER in entry

        captured = capsys.readouterr()
        assert 'installed' in captured.out

    @patch('lib.backup._write_crontab')
    @patch('lib.backup._read_crontab', return_value=[
        '0 * * * * /bin/claudio backup /old --hours 24 --days 7 >> /log 2>&1 # claudio-backup',
        '30 * * * * /bin/other-job',
    ])
    def test_replaces_existing_entry(self, mock_read, mock_write, tmp_path):
        dest = str(tmp_path)
        result = backup.backup_cron_install(
            dest, claudio_bin='/bin/claudio', claudio_path='/home/.claudio',
        )
        assert result == 0

        written_lines = mock_write.call_args[0][0]
        # Should have the other job plus the new entry
        assert len(written_lines) == 2
        assert '30 * * * * /bin/other-job' in written_lines
        marker_entries = [x for x in written_lines if backup.BACKUP_CRON_MARKER in x]
        assert len(marker_entries) == 1

    def test_empty_dest_returns_error(self, capsys):
        result = backup.backup_cron_install('')
        assert result == 1
        captured = capsys.readouterr()
        assert 'required' in captured.err

    def test_nonexistent_dest_returns_error(self, capsys):
        result = backup.backup_cron_install('/nonexistent/path')
        assert result == 1
        captured = capsys.readouterr()
        assert 'does not exist' in captured.err

    @patch('lib.backup._write_crontab')
    @patch('lib.backup._read_crontab', return_value=[])
    def test_rejects_unsafe_path(self, mock_read, mock_write, tmp_path, capsys):
        # Create a directory with a safe name, but pass an unsafe resolved path
        # by using a path with a space that realpath would preserve
        dest = str(tmp_path / 'safe')
        os.makedirs(dest)

        # Patch os.path.realpath to return an unsafe path
        with patch('lib.backup.os.path.realpath', return_value='/mnt/my backups'):
            result = backup.backup_cron_install(dest)
            assert result == 1

        captured = capsys.readouterr()
        assert 'invalid characters' in captured.err
        mock_write.assert_not_called()


# -- backup_cron_uninstall --


class TestBackupCronUninstall:
    @patch('lib.backup._write_crontab')
    @patch('lib.backup._read_crontab', return_value=[
        '0 * * * * /bin/claudio backup /dest --hours 24 --days 7 >> /log 2>&1 # claudio-backup',
        '30 * * * * /bin/other-job',
    ])
    def test_removes_backup_entry(self, mock_read, mock_write, capsys):
        result = backup.backup_cron_uninstall()
        assert result == 0

        written_lines = mock_write.call_args[0][0]
        assert len(written_lines) == 1
        assert '30 * * * * /bin/other-job' in written_lines

        captured = capsys.readouterr()
        assert 'removed' in captured.out

    @patch('lib.backup._write_crontab')
    @patch('lib.backup._read_crontab', return_value=[
        '30 * * * * /bin/other-job',
    ])
    def test_no_entry_to_remove(self, mock_read, mock_write, capsys):
        result = backup.backup_cron_uninstall()
        assert result == 0

        mock_write.assert_not_called()
        captured = capsys.readouterr()
        assert 'No backup cron job found' in captured.out

    @patch('lib.backup._write_crontab')
    @patch('lib.backup._read_crontab', return_value=[])
    def test_empty_crontab(self, mock_read, mock_write, capsys):
        result = backup.backup_cron_uninstall()
        assert result == 0
        mock_write.assert_not_called()


# -- _read_crontab / _write_crontab --


class TestCrontabHelpers:
    @patch('lib.backup.subprocess.run')
    def test_read_crontab_returns_lines(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='0 * * * * /bin/job1\n30 * * * * /bin/job2\n',
        )
        lines = backup._read_crontab()
        assert lines == ['0 * * * * /bin/job1', '30 * * * * /bin/job2']

    @patch('lib.backup.subprocess.run')
    def test_read_crontab_no_crontab(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout='')
        lines = backup._read_crontab()
        assert lines == []

    @patch('lib.backup.subprocess.run')
    def test_read_crontab_command_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        lines = backup._read_crontab()
        assert lines == []

    @patch('lib.backup.subprocess.run')
    def test_write_crontab_sends_input(self, mock_run):
        backup._write_crontab(['0 * * * * /bin/job1', '30 * * * * /bin/job2'])
        mock_run.assert_called_once_with(
            ['crontab', '-'],
            input='0 * * * * /bin/job1\n30 * * * * /bin/job2\n',
            text=True, timeout=10,
        )

    @patch('lib.backup.subprocess.run')
    def test_write_crontab_empty_clears(self, mock_run):
        backup._write_crontab([])
        mock_run.assert_called_once_with(
            ['crontab', '-'],
            input='', text=True, timeout=10,
        )
