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
            cp -al "$oldest_today" "$daily_today"
            log "backup" "Daily backup promoted: $daily_today"
        fi
    fi

    # --- Rotate hourly backups ---
    _backup_rotate "$hourly_dir" "$max_hourly" "latest"

    # --- Rotate daily backups ---
    _backup_rotate "$daily_dir" "$max_daily"

    echo "Backup complete: $new_hourly"
}

_backup_rotate() {
    local dir="$1"
    local keep="$2"
    local skip_name="${3:-}"

    local -a snapshots=()
    local entry
    while IFS= read -r entry; do
        [[ -z "$entry" ]] && continue
        local name
        name=$(basename "$entry")
        if [[ -n "$skip_name" && "$name" == "$skip_name" ]]; then
            continue
        fi
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
        local hourly_count
        hourly_count=$(find "$hourly_dir" -maxdepth 1 -mindepth 1 -type d | wc -l)
        echo "Hourly backups: $hourly_count"
        if (( hourly_count > 0 )); then
            local oldest newest
            oldest=$(find "$hourly_dir" -maxdepth 1 -mindepth 1 -type d | sort | head -1 | xargs basename)
            newest=$(find "$hourly_dir" -maxdepth 1 -mindepth 1 -type d | sort | tail -1 | xargs basename)
            echo "  Oldest: $oldest"
            echo "  Newest: $newest"
        fi
    else
        echo "Hourly backups: 0"
    fi

    echo ""

    if [[ -d "$daily_dir" ]]; then
        local daily_count
        daily_count=$(find "$daily_dir" -maxdepth 1 -mindepth 1 -type d | wc -l)
        echo "Daily backups: $daily_count"
        if (( daily_count > 0 )); then
            local oldest newest
            oldest=$(find "$daily_dir" -maxdepth 1 -mindepth 1 -type d | sort | head -1 | xargs basename)
            newest=$(find "$daily_dir" -maxdepth 1 -mindepth 1 -type d | sort | tail -1 | xargs basename)
            echo "  Oldest: $oldest"
            echo "  Newest: $newest"
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

    local claudio_bin
    claudio_bin="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/claudio"

    local cron_entry="0 * * * * ${claudio_bin} backup ${dest} --hours ${max_hourly} --days ${max_daily} >> ${CLAUDIO_PATH}/cron.log 2>&1 ${BACKUP_CRON_MARKER}"

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
