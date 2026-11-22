#!/usr/bin/env bash
# Timestamped log lines for the tile scripts (Hugo, 20 Nov 2026: every logging
# statement carries a timestamp, so a pasted log says when each step ran).
#
#     log "==> uploading to R2"           # 2026-11-20 12:44:31 ==> uploading to R2
#     log "error: no grid.raw" >&2        # the redirect applies as for echo
#     logf '  ok    %s (%s m)' "$a" "$b"  # printf-style; the newline is added
#     some_command | stamp_lines          # stamps every line a tool prints
#
# Local time, to the second, with the date: a terrain run spans days. `date`
# rather than bash's printf %(...)T, which the bash 3.2 on macOS lacks.
#
# Sourced by config.sh; not meant to be run directly.

log_stamp() { date '+%Y-%m-%d %H:%M:%S'; }

log() { printf '%s %s\n' "$(log_stamp)" "$*"; }

logf() {
    local format=$1
    shift
    # shellcheck disable=SC2059  # the format is the caller's, by design
    printf "%s ${format}\n" "$(log_stamp)" "$@"
}

stamp_lines() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        log "$line"
    done
}
