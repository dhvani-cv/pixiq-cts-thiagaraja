#!/usr/bin/env bash
#
# Deletes the oldest dated subfolders under audit/ and debug/ (folders named
# YYYY-MM-DD) whenever free disk space on the target filesystem drops below
# THRESHOLD_GB. Keeps deleting oldest-first, across both trees, until free
# space is back above the threshold or there is nothing left to delete.
#
# This script lives in a "cleanup" folder inside the project (e.g.
# .../cone-transport-system-pixiq/cleanup/cleanup_audit_debug.sh). The log
# file is written next to it in that same folder.
#
# Install as a cron job, e.g. every 4 hours:
#   0 */4 * * * /home/pixiq/cone-transport-system-pixiq/cleanup/cleanup_audit_debug.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
BASE="$(dirname -- "$SCRIPT_DIR")"   # project root = parent of the cleanup/ folder
BASE_DIRS=("$BASE/audit" "$BASE/debug")
FS_PATH="/"                 # filesystem whose free space we check (df)
THRESHOLD_GB=10
LOG_FILE="$SCRIPT_DIR/cleanup.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

avail_gb() {
    df --output=avail -BG "$FS_PATH" | tail -1 | tr -dc '0-9'
}

oldest_date_dir() {
    # Lists top-level YYYY-MM-DD dirs from both BASE_DIRS, picks the oldest by name.
    {
        for d in "${BASE_DIRS[@]}"; do
            [ -d "$d" ] || continue
            find "$d" -maxdepth 1 -mindepth 1 -type d -regextype posix-extended \
                -regex '.*/[0-9]{4}-[0-9]{2}-[0-9]{2}'
        done
    } | awk -F/ '{print $NF"|"$0}' | sort -t'|' -k1,1 | head -1 | cut -d'|' -f2-
}

current=$(avail_gb)
log "check: avail=${current}G threshold=${THRESHOLD_GB}G"

if [ "$current" -ge "$THRESHOLD_GB" ]; then
    log "ok: nothing to do"
    exit 0
fi

while [ "$current" -lt "$THRESHOLD_GB" ]; do
    target=$(oldest_date_dir)
    if [ -z "$target" ]; then
        log "warn: avail=${current}G still below threshold but no more dated folders to delete"
        break
    fi
    size=$(du -sh "$target" 2>/dev/null | cut -f1)
    rm -rf -- "$target"
    log "deleted: $target (freed ~${size})"
    current=$(avail_gb)
    log "check: avail=${current}G threshold=${THRESHOLD_GB}G"
done

log "done: avail=${current}G"
