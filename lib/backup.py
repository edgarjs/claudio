"""Backup management for Claudio -- hourly and daily rotating backups."""

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from lib.util import log, log_error, print_error, print_success

BACKUP_CRON_MARKER = "# claudio-backup"

# Reject shell metacharacters in paths used for cron entries
_SAFE_PATH_RE = re.compile(r'^[a-zA-Z0-9_./-]+$')


def _check_mount(dest):
    """Check whether dest sits on a mounted filesystem.

    Only checks paths under /mnt/* or /media/* (external drive paths).
    Returns True if mounted (or path is not an external drive path),
    False if the drive appears disconnected.
    """
    if not dest.startswith('/mnt/') and not dest.startswith('/media/'):
        return True

    # Try findmnt first
    try:
        result = subprocess.run(
            ['findmnt', '--target', dest, '-n', '-o', 'TARGET'],
            capture_output=True, text=True, timeout=10,
        )
        target = result.stdout.strip()
        if target == '/' or not target:
            return False
        return True
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        return False

    # Fallback: mountpoint on the first two path components (e.g. /mnt/ssd)
    try:
        parts = dest.strip('/').split('/')
        if len(parts) >= 2:
            mount_root = '/' + '/'.join(parts[:2])
            result = subprocess.run(
                ['mountpoint', '-q', mount_root],
                capture_output=True, timeout=10,
            )
            return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Can't determine mount status -- assume mounted
    return True


def _safe_dest_path(dest):
    """Validate that dest contains no shell metacharacters.

    Returns True if the path is safe for cron entries, False otherwise.
    """
    return bool(_SAFE_PATH_RE.match(dest))


def _backup_rotate(directory, keep):
    """Rotate snapshots in directory, keeping only the N most recent.

    Snapshots are directories with timestamp names (YYYY-MM-DD_HHMM or
    YYYY-MM-DD), sorted lexicographically. Oldest are removed first.
    Symlinks (like 'latest') are excluded from the count and never deleted.
    """
    if not os.path.isdir(directory):
        return

    entries = sorted(
        e for e in os.listdir(directory)
        if os.path.isdir(os.path.join(directory, e))
        and not os.path.islink(os.path.join(directory, e))
    )

    if len(entries) > keep:
        to_remove = len(entries) - keep
        for name in entries[:to_remove]:
            full_path = os.path.join(directory, name)
            shutil.rmtree(full_path)
            log('backup', f'Rotated out: {full_path}')


def backup_run(dest, max_hourly=24, max_daily=7, claudio_path=None):
    """Run a backup: hourly snapshot with rsync hardlinks, daily promotion.

    Args:
        dest: Backup destination directory.
        max_hourly: Number of hourly snapshots to retain.
        max_daily: Number of daily snapshots to retain.
        claudio_path: Source directory to back up. Defaults to ~/.claudio/.

    Returns:
        0 on success, 1 on error.
    """
    if claudio_path is None:
        claudio_path = os.path.join(str(Path.home()), '.claudio')

    if not dest:
        print_error('backup destination is required.')
        return 1

    if not os.path.isdir(dest):
        print_error(f"destination '{dest}' does not exist or is not a directory.")
        return 1

    if not _check_mount(dest):
        print_error(f"'{dest}' is not on a mounted filesystem. Is the drive connected?")
        return 1

    # Resolve to absolute path
    dest = os.path.realpath(dest)

    backup_root = os.path.join(dest, 'claudio-backups')
    hourly_dir = os.path.join(backup_root, 'hourly')
    daily_dir = os.path.join(backup_root, 'daily')
    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M')

    os.makedirs(hourly_dir, exist_ok=True)
    os.makedirs(daily_dir, exist_ok=True)

    # --- Hourly backup using rsync with hardlinks ---
    latest_hourly = os.path.join(hourly_dir, 'latest')
    new_hourly = os.path.join(hourly_dir, timestamp)

    rsync_args = ['rsync', '-a', '--delete']
    if os.path.isdir(latest_hourly):
        rsync_args.extend(['--link-dest', latest_hourly])

    # Trailing slash on source means "contents of", matching the bash version
    src = claudio_path.rstrip('/') + '/'
    rsync_args.extend([src, new_hourly + '/'])

    result = subprocess.run(rsync_args, capture_output=True, text=True)
    if result.returncode != 0:
        log_error('backup', f'rsync failed for hourly backup: {result.stderr.strip()}')
        print_error('rsync failed.')
        return 1

    # Update 'latest' symlink
    if os.path.islink(latest_hourly):
        os.unlink(latest_hourly)
    elif os.path.exists(latest_hourly):
        os.unlink(latest_hourly)
    os.symlink(new_hourly, latest_hourly)
    log('backup', f'Hourly backup created: {new_hourly}')

    # --- Promote oldest hourly to daily (once per day) ---
    today = datetime.now().strftime('%Y-%m-%d')
    daily_today = os.path.join(daily_dir, today)

    if not os.path.isdir(daily_today):
        # Find the oldest hourly backup from today to promote
        todays_hourlies = sorted(
            e for e in os.listdir(hourly_dir)
            if e.startswith(today + '_')
            and os.path.isdir(os.path.join(hourly_dir, e))
        )

        if todays_hourlies:
            oldest_today = os.path.join(hourly_dir, todays_hourlies[0])

            # cp -al (hardlinks) is GNU-only; fall back to rsync --link-dest
            cp_result = subprocess.run(
                ['cp', '-al', oldest_today, daily_today],
                capture_output=True, text=True,
            )
            if cp_result.returncode != 0:
                rsync_result = subprocess.run(
                    ['rsync', '-a', '--link-dest', oldest_today,
                     oldest_today + '/', daily_today + '/'],
                    capture_output=True, text=True,
                )
                if rsync_result.returncode != 0:
                    log_error('backup', 'rsync failed promoting daily backup')
                    return 1

            log('backup', f'Daily backup promoted: {daily_today}')

    # --- Rotate ---
    _backup_rotate(hourly_dir, max_hourly)
    _backup_rotate(daily_dir, max_daily)

    print(f'Backup complete: {new_hourly}')
    return 0


def backup_status(dest):
    """Display backup status for a destination.

    Args:
        dest: Backup destination directory.

    Returns:
        0 on success, 1 on error.
    """
    if not dest:
        print_error('backup destination is required.')
        return 1

    backup_root = os.path.join(dest, 'claudio-backups')

    if not os.path.isdir(backup_root):
        print(f'No backups found at {backup_root}')
        return 0

    hourly_dir = os.path.join(backup_root, 'hourly')
    daily_dir = os.path.join(backup_root, 'daily')

    print(f'Backup location: {backup_root}')
    print()

    # Hourly snapshot info
    if os.path.isdir(hourly_dir):
        snapshots = sorted(
            e for e in os.listdir(hourly_dir)
            if os.path.isdir(os.path.join(hourly_dir, e))
            and not os.path.islink(os.path.join(hourly_dir, e))
        )
        count = len(snapshots)
        print(f'Hourly backups: {count}')
        if count > 0:
            print(f'  Oldest: {snapshots[0]}')
            print(f'  Newest: {snapshots[-1]}')
    else:
        print('Hourly backups: 0')

    print()

    # Daily snapshot info
    if os.path.isdir(daily_dir):
        snapshots = sorted(
            e for e in os.listdir(daily_dir)
            if os.path.isdir(os.path.join(daily_dir, e))
            and not os.path.islink(os.path.join(daily_dir, e))
        )
        count = len(snapshots)
        print(f'Daily backups: {count}')
        if count > 0:
            print(f'  Oldest: {snapshots[0]}')
            print(f'  Newest: {snapshots[-1]}')
    else:
        print('Daily backups: 0')

    print()

    # Total size
    try:
        result = subprocess.run(
            ['du', '-sh', backup_root],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            size = result.stdout.split('\t')[0].strip()
            print(f'Total size: {size}')
        else:
            print('Total size: unknown')
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print('Total size: unknown')

    return 0


def backup_cron_install(dest, max_hourly=24, max_daily=7, claudio_bin=None,
                        claudio_path=None):
    """Install an hourly backup cron job.

    Args:
        dest: Backup destination directory.
        max_hourly: Number of hourly snapshots to retain.
        max_daily: Number of daily snapshots to retain.
        claudio_bin: Path to the claudio executable. Auto-detected if None.
        claudio_path: CLAUDIO_PATH for cron log location. Defaults to ~/.claudio/.

    Returns:
        0 on success, 1 on error.
    """
    if claudio_path is None:
        claudio_path = os.path.join(str(Path.home()), '.claudio')

    if not dest:
        print_error('backup destination is required.')
        return 1

    if not os.path.isdir(dest):
        print_error(f"destination '{dest}' does not exist or is not a directory.")
        return 1

    # Resolve to absolute path
    dest = os.path.realpath(dest)

    if not _safe_dest_path(dest):
        print_error('backup destination contains invalid characters.')
        return 1

    if claudio_bin is None:
        # Compute from this file's location: lib/backup.py -> ../claudio
        claudio_bin = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'claudio',
        )

    cron_entry = (
        f'0 * * * * {claudio_bin} backup {dest}'
        f' --hours {max_hourly} --days {max_daily}'
        f' >> {claudio_path}/cron.log 2>&1 {BACKUP_CRON_MARKER}'
    )

    # Read existing crontab, remove old claudio-backup entries, add new one
    existing = _read_crontab()
    filtered = [line for line in existing if BACKUP_CRON_MARKER not in line]
    filtered.append(cron_entry)
    _write_crontab(filtered)

    print_success('Backup cron job installed (runs every hour).')
    print(f'  Destination: {dest}')
    print(f'  Hourly retention: {max_hourly}')
    print(f'  Daily retention: {max_daily}')
    return 0


def backup_cron_uninstall():
    """Remove the backup cron job.

    Returns:
        0 on success.
    """
    existing = _read_crontab()
    filtered = [line for line in existing if BACKUP_CRON_MARKER not in line]

    if len(filtered) < len(existing):
        _write_crontab(filtered)
        print_success('Backup cron job removed.')
    else:
        print('No backup cron job found.')

    return 0


def _read_crontab():
    """Read the current user's crontab, returning a list of lines.

    Returns an empty list if no crontab exists.
    """
    try:
        result = subprocess.run(
            ['crontab', '-l'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return [line for line in result.stdout.splitlines() if line]
        return []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _write_crontab(lines):
    """Write lines as the current user's crontab."""
    content = '\n'.join(lines) + '\n' if lines else ''
    subprocess.run(
        ['crontab', '-'],
        input=content, text=True, timeout=10,
    )
