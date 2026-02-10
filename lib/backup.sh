#!/bin/bash

# Backup management for Claudio
# Maintains hourly and daily rotating backups of ~/.claudio

# shellcheck source=lib/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

BACKUP_CRON_MARKER="# claudio-backup"

backup_run() {
    local dest="$1"
    local max_hourly="${2:-24}"
    local max_daily="${3:-7}"

    if [[ -z "$dest" ]]; then
        echo "Error: backup destination is required." >&2
        return 1
    fi

    if [[ ! -d "$dest" ]]; then
        echo "Error: destination '$dest' does not exist or is not a directory." >&2
        return 1
    fi

    # Verify the destination is a mount point when it looks like an external
    # drive path (/mnt/*, /media/*). Catches disconnected drives that leave
    # an empty mount point directory behind.
    if [[ "$dest" == /mnt/* || "$dest" == /media/* ]]; then
        if command -v mountpoint >/dev/null 2>&1 && ! mountpoint -q "$dest" 2>/dev/null; then
            echo "Error: '$dest' is not a mounted filesystem. Is the drive connected?" >&2
            return 1
        fi
    fi

    # Resolve to absolute path (important for cron context)
    dest="$(cd "$dest" && pwd)"

    local backup_root="$dest/claudio-backups"
    local hourly_dir="$backup_root/hourly"
    local daily_dir="$backup_root/daily"
    local timestamp
    timestamp=$(date '+%Y-%m-%d_%H%M')

    mkdir -p "$hourly_dir" "$daily_dir"

    # --- Hourly backup using rsync with hardlinks ---
    local latest_hourly="$hourly_dir/latest"
    local new_hourly="$hourly_dir/$timestamp"

    local rsync_args=(-a --delete)
    if [[ -d "$latest_hourly" ]]; then
        rsync_args+=(--link-dest="$latest_hourly")
    fi

    if rsync "${rsync_args[@]}" "$CLAUDIO_PATH/" "$new_hourly/"; then
        rm -f "$latest_hourly"
        ln -s "$new_hourly" "$latest_hourly"
        log "backup" "Hourly backup created: $new_hourly"
    else
        log_error "backup" "rsync failed for hourly backup"
        echo "Error: rsync failed." >&2
        return 1
    fi

    # --- Promote oldest hourly to daily (once per day) ---
    local today
    today=$(date '+%Y-%m-%d')
    local daily_today="$daily_dir/$today"

    if [[ ! -d "$daily_today" ]]; then
        # Find the oldest hourly backup from today to promote
        local oldest_today
        oldest_today=$(find "$hourly_dir" -maxdepth 1 -mindepth 1 -name "${today}_*" -type d | sort | head -1)

        if [[ -n "$oldest_today" ]]; then
            # cp -al (hardlinks) is GNU-only; macOS needs rsync --link-dest fallback
            if cp -al "$oldest_today" "$daily_today" 2>/dev/null; then
                : # GNU cp hardlink succeeded
            else
                # Fallback: rsync with hardlinks (portable)
                if ! rsync -a --link-dest="$oldest_today" "$oldest_today/" "$daily_today/"; then
                    log_error "backup" "rsync failed promoting daily backup"
                    return 1
                fi
            fi
            log "backup" "Daily backup promoted: $daily_today"
        fi
    fi

    # --- Rotate hourly backups ---
    _backup_rotate "$hourly_dir" "$max_hourly"

    # --- Rotate daily backups ---
    _backup_rotate "$daily_dir" "$max_daily"

    echo "Backup complete: $new_hourly"
}

_backup_rotate() {
    local dir="$1"
    local keep="$2"

    local -a snapshots=()
    local entry
    while IFS= read -r entry; do
        [[ -z "$entry" ]] && continue
        snapshots+=("$entry")
    done < <(find "$dir" -maxdepth 1 -mindepth 1 -type d | sort)

    local count=${#snapshots[@]}
    if (( count > keep )); then
        local to_remove=$(( count - keep ))
        for (( i = 0; i < to_remove; i++ )); do
            rm -rf "${snapshots[$i]}"
            log "backup" "Rotated out: ${snapshots[$i]}"
        done
    fi
}

backup_status() {
    local dest="$1"

    if [[ -z "$dest" ]]; then
        echo "Error: backup destination is required." >&2
        return 1
    fi

    local backup_root="$dest/claudio-backups"

    if [[ ! -d "$backup_root" ]]; then
        echo "No backups found at $backup_root"
        return 0
    fi

    local hourly_dir="$backup_root/hourly"
    local daily_dir="$backup_root/daily"

    echo "Backup location: $backup_root"
    echo ""

    if [[ -d "$hourly_dir" ]]; then
        local -a hourly_snapshots=()
        local _entry
        while IFS= read -r _entry; do
            [[ -z "$_entry" ]] && continue
            hourly_snapshots+=("$_entry")
        done < <(find "$hourly_dir" -maxdepth 1 -mindepth 1 -type d | sort)
        local hourly_count=${#hourly_snapshots[@]}
        echo "Hourly backups: $hourly_count"
        if (( hourly_count > 0 )); then
            echo "  Oldest: $(basename "${hourly_snapshots[0]}")"
            echo "  Newest: $(basename "${hourly_snapshots[$((hourly_count - 1))]}")"
        fi
    else
        echo "Hourly backups: 0"
    fi

    echo ""

    if [[ -d "$daily_dir" ]]; then
        local -a daily_snapshots=()
        local _entry
        while IFS= read -r _entry; do
            [[ -z "$_entry" ]] && continue
            daily_snapshots+=("$_entry")
        done < <(find "$daily_dir" -maxdepth 1 -mindepth 1 -type d | sort)
        local daily_count=${#daily_snapshots[@]}
        echo "Daily backups: $daily_count"
        if (( daily_count > 0 )); then
            echo "  Oldest: $(basename "${daily_snapshots[0]}")"
            echo "  Newest: $(basename "${daily_snapshots[$((daily_count - 1))]}")"
        fi
    else
        echo "Daily backups: 0"
    fi

    echo ""
    local total_size
    total_size=$(du -sh "$backup_root" 2>/dev/null | cut -f1)
    echo "Total size: $total_size"
}

backup_cron_install() {
    local dest="$1"
    local max_hourly="${2:-24}"
    local max_daily="${3:-7}"

    if [[ -z "$dest" ]]; then
        echo "Error: backup destination is required." >&2
        return 1
    fi

    if [[ ! -d "$dest" ]]; then
        echo "Error: destination '$dest' does not exist or is not a directory." >&2
        return 1
    fi

    # Resolve to absolute path
    dest="$(cd "$dest" && pwd)"

    # Validate dest path: reject shell metacharacters, newlines, and cron-special chars
    if [[ "$dest" =~ [^a-zA-Z0-9_./-] ]]; then
        echo "Error: backup destination contains invalid characters." >&2
        return 1
    fi

    local claudio_bin
    claudio_bin="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/claudio"

    local cron_entry
    cron_entry="$(printf '0 * * * * %q backup %q --hours %d --days %d >> %q/cron.log 2>&1 %s' \
        "$claudio_bin" "$dest" "$max_hourly" "$max_daily" "$CLAUDIO_PATH" "$BACKUP_CRON_MARKER")"

    (crontab -l 2>/dev/null | grep -v "$BACKUP_CRON_MARKER"; echo "$cron_entry") | crontab -
    print_success "Backup cron job installed (runs every hour)."
    echo "  Destination: $dest"
    echo "  Hourly retention: $max_hourly"
    echo "  Daily retention: $max_daily"
}

backup_cron_uninstall() {
    if crontab -l 2>/dev/null | grep -q "$BACKUP_CRON_MARKER"; then
        crontab -l 2>/dev/null | grep -v "$BACKUP_CRON_MARKER" | crontab -
        print_success "Backup cron job removed."
    else
        echo "No backup cron job found."
    fi
}
