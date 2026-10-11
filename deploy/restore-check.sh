#!/usr/bin/env bash
# lead-bot/deploy/restore-check.sh
# Proves a backup can actually be restored: pulls the newest snapshot of one client's database
# into a scratch folder, checks it, and prints what's in it. A backup nobody has restored is
# only a hope. Safe to run any time: it never touches the live database.
#
#   RESTIC_REPOSITORY=... RESTIC_PASSWORD_FILE=... bash deploy/restore-check.sh <client> [scratch_dir]
#
# Passes (exit 0) when the restored file's PRAGMA integrity_check is "ok". Prints the lead and
# text counts so you can compare them with the live /health counts of that client. Leaves the
# restored file in the scratch folder (a new temporary one unless you name it) and prints where.
set -u -o pipefail
umask 077

ROOT="${LEAD_BOT_ROOT:-/srv/lead-bot}"
BACKUP_HOST="${BACKUP_HOST:-lead-bot}"
client="${1:?usage: restore-check.sh <client> [scratch_dir]}"
scratch="${2:-$(mktemp -d)}"

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY}"

mkdir -p "$scratch"
# restic stores each file under its original absolute path, so ask for those paths. The client's
# .env and client.toml come from the same snapshot as the database.
wanted="$ROOT/backups/$client/lead_bot.db"
if ! restic restore latest --host "$BACKUP_HOST" --target "$scratch" --include "$wanted" \
  --include "$ROOT/clients/$client/.env" --include "$ROOT/clients/$client/client.toml" --quiet; then
  echo "FAILED  restic could not restore $wanted (is there a snapshot for this client?)" >&2
  exit 1
fi
restored="$scratch$wanted"
[ -f "$restored" ] || { echo "FAILED  restore finished but $restored is missing" >&2; exit 1; }
# A snapshot without a client's .env or client.toml can restore the data but not the client: the
# keys and the roofer's facts are gone. Check they came back and aren't empty, without printing them.
for config_name in .env client.toml; do
  restored_config="$scratch$ROOT/clients/$client/$config_name"
  if [ ! -s "$restored_config" ]; then
    echo "FAILED  $client: $config_name is missing or empty in the snapshot" >&2
    exit 1
  fi
done

integrity="$(sqlite3 "$restored" "PRAGMA integrity_check" 2>&1 | head -n 1)"
if [ "$integrity" != "ok" ]; then
  echo "FAILED  $client: restored database failed its integrity check: $integrity" >&2
  exit 1
fi

echo "ok      $client: restored $restored"
echo "        integrity_check: ok"
echo "        config files: .env and client.toml restored (contents not shown)"
echo "        leads: $(sqlite3 "$restored" 'SELECT COUNT(*) FROM leads')"
echo "        texts: $(sqlite3 "$restored" 'SELECT COUNT(*) FROM texts')"
echo "        newest lead: $(sqlite3 "$restored" "SELECT COALESCE(datetime(MAX(created_at), 'unixepoch'), 'none') FROM leads") UTC"
echo "        the scratch folder holds that client's keys now: delete it when you are done ($scratch)"
