#!/usr/bin/env bash
# lead-bot/deploy/run-backup.sh
# The nightly entry point for cron: runs deploy/backup.sh with the right settings, one at a time,
# with a log, and (optionally) tells a monitoring service whether it worked.
#
#   bash deploy/run-backup.sh
#
# Settings come from /srv/lead-bot/backup.env (mode 600, KEY=VALUE lines, never run as code):
#   RESTIC_REPOSITORY      where the encrypted copies go
#   RESTIC_PASSWORD_FILE   file holding the repository password
#   (for S3/B2 storage, that service's credentials go here too, e.g. AWS_ACCESS_KEY_ID)
#   BACKUP_PING_URL        optional "dead man's switch" URL (Healthchecks.io style): GET <url> after a
#                          good run, GET <url>/fail after a bad one. A monitor that expects a ping
#                          every day also notices when this job silently stops running.
#
# Exit codes: backup.sh's own (0 ok, 1 something failed, 2 can't start), or 3 when another backup
# was still running (nothing is done then; a run that long deserves a look).
#
# Other settings (environment): LEAD_BOT_ROOT (default /srv/lead-bot), BACKUP_SETTINGS (a different
# settings file; used by the tests).
set -u
umask 077   # the log names clients and the settings file holds keys

# Cron starts jobs with almost no environment, so set what the tools need.
export PATH=/usr/local/bin:/usr/bin:/bin
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${LEAD_BOT_ROOT:-/srv/lead-bot}"
SETTINGS="${BACKUP_SETTINGS:-$ROOT/backup.env}"
LOG="$ROOT/backups/backup.log"
LOCK="$ROOT/backups/.backup.lock"
LOG_LINES_KEPT=2000

mkdir -p "$ROOT/backups"
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG"; }

[ -r "$SETTINGS" ] || { log "FAILED no readable settings file at $SETTINGS"; echo "no readable settings file at $SETTINGS" >&2; exit 2; }

# Read KEY=VALUE lines one at a time and export them. The file is deliberately not sourced: a stray
# or edited line in it must not be able to run as a command.
BACKUP_PING_URL=""
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|'#'*) continue ;;
    [A-Za-z_][A-Za-z0-9_]*=*) export "${line%%=*}=${line#*=}" ;;
    *) log "ignored a settings line that is not KEY=VALUE" ;;
  esac
done < "$SETTINGS"

ping_monitor() { # ping_monitor <suffix>: tell the monitor how it went; never let this fail the backup
  [ -n "${BACKUP_PING_URL:-}" ] || return 0
  curl -fsS -m 15 --retry 2 -o /dev/null "${BACKUP_PING_URL%/}$1" 2>>"$LOG" || log "could not reach the monitor at the ping URL"
}

# One backup at a time. A second cron start while the first still runs just says so and leaves.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "SKIPPED another backup is still running"
  echo "another backup is still running" >&2
  exit 3
fi

log "START"
bash "$DEPLOY_DIR/backup.sh" >> "$LOG" 2>&1
status=$?
if [ "$status" -eq 0 ]; then
  log "OK finished (exit 0)"; ping_monitor ""
else
  log "FAILED finished (exit $status)"; ping_monitor "/fail"
fi

# Keep the log from growing forever.
tail -n "$LOG_LINES_KEPT" "$LOG" > "$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
exit "$status"
