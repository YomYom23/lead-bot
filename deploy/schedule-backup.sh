#!/usr/bin/env bash
# lead-bot/deploy/schedule-backup.sh
# Puts the nightly backup into the deploy user's crontab (no root needed), or takes it out.
#
#   bash deploy/schedule-backup.sh install [HH:MM]   default 03:17, in the SERVER's clock (UTC on the VPS)
#   bash deploy/schedule-backup.sh remove
#   bash deploy/schedule-backup.sh status
#
# Safe to run again: install replaces the line it made before and never touches other crontab lines.
# Why cron and not a systemd timer: a system timer needs root, and a user timer only runs after
# logout when "linger" is on, which also needs root. The deploy user has neither.
set -u

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER="# lead-bot-backup"
usage() { echo "usage: schedule-backup.sh install [HH:MM] | remove | status" >&2; exit 2; }
command -v crontab >/dev/null 2>&1 || { echo "crontab is not installed" >&2; exit 1; }

other_lines() { crontab -l 2>/dev/null | grep -vF "$MARKER"; }   # everything except our own line

case "${1:-}" in
  install)
    time_of_day="${2:-03:17}"
    printf '%s' "$time_of_day" | grep -Eq '^([01][0-9]|2[0-3]):[0-5][0-9]$' || { echo "time must be HH:MM (24 hour): '$time_of_day'" >&2; exit 2; }
    hour="${time_of_day%:*}"; minute="${time_of_day#*:}"
    # 10# stops "08" being read as an octal number.
    entry="$((10#$minute)) $((10#$hour)) * * * $DEPLOY_DIR/run-backup.sh >/dev/null 2>&1 $MARKER"
    { other_lines; echo "$entry"; } | crontab - || { echo "could not write the crontab" >&2; exit 1; }
    echo "installed: $entry"
    ;;
  remove)
    other_lines | crontab - 2>/dev/null || crontab -r 2>/dev/null
    echo "removed the lead-bot backup line (other lines were left alone)"
    ;;
  status)
    line="$(crontab -l 2>/dev/null | grep -F "$MARKER" || true)"
    if [ -n "$line" ]; then echo "scheduled: $line"; else echo "not scheduled"; fi
    echo "cron service: $(systemctl is-active cron 2>&1)"
    ;;
  *) usage ;;
esac
