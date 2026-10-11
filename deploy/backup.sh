#!/usr/bin/env bash
# lead-bot/deploy/backup.sh
# Nightly backup of every client's database, then an encrypted copy off the server.
#
#   RESTIC_REPOSITORY=... RESTIC_PASSWORD_FILE=... bash deploy/backup.sh
#
# 1. For each /srv/lead-bot/clients/<client>/data/lead_bot.db: take a consistent copy with
#    sqlite3's .backup into /srv/lead-bot/backups/<client>/lead_bot.db. Copying the file with
#    `cp` is NOT safe: the bot runs in WAL mode, so recent writes live in lead_bot.db-wal until
#    SQLite folds them in, and a plain copy of the .db alone is missing them (or is torn, if the
#    bot writes mid-copy). .backup reads through SQLite itself, so it sees one moment in time
#    and never blocks the bot's writes.
# 2. Check the copy (PRAGMA integrity_check) before trusting it.
# 3. restic uploads the copies, encrypted, to off-server storage and thins old snapshots:
#    14 daily and 6 monthly are kept.
#
# One client failing never stops the others; the exit code is non-zero if anything failed, so the
# systemd timer (deploy step 9) can alert. Run it as the user that owns the data/ folders
# (UID 1000), or with sudo: SQLite has to open the database file to read it.
#
# Settings (environment):
#   RESTIC_REPOSITORY      where the encrypted copies go (an S3/B2 address, or a folder in tests)
#   RESTIC_PASSWORD_FILE   file holding the repository password (or set RESTIC_PASSWORD). LOSE THIS
#                          AND THE BACKUPS ARE UNREADABLE: keep a copy somewhere that isn't the VPS.
#   LEAD_BOT_ROOT          default /srv/lead-bot
#   BACKUP_HOST            name restic files snapshots under (default lead-bot); fixed so a
#                          changed hostname never splits the history in two
set -u -o pipefail
umask 077   # the copies hold homeowners' names, phones and addresses: owner-only

ROOT="${LEAD_BOT_ROOT:-/srv/lead-bot}"
CLIENTS_DIR="$ROOT/clients"
BACKUP_DIR="$ROOT/backups"
BACKUP_HOST="${BACKUP_HOST:-lead-bot}"
KEEP_DAILY=14
KEEP_MONTHLY=6

failed=0
succeeded=0
fail() { echo "FAILED  $1" >&2; failed=$((failed + 1)); }

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY (where the off-server copies go)}"
if [ -z "${RESTIC_PASSWORD_FILE:-}" ] && [ -z "${RESTIC_PASSWORD:-}" ]; then
  echo "set RESTIC_PASSWORD_FILE (or RESTIC_PASSWORD) so restic can encrypt the copies" >&2
  exit 2
fi
for tool in sqlite3 restic; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool is not installed (apt install $tool)" >&2; exit 2; }
done
[ -d "$CLIENTS_DIR" ] || { echo "no clients folder at $CLIENTS_DIR" >&2; exit 2; }

# A repository nobody initialised is almost always a wrong RESTIC_REPOSITORY, so stop and say so
# rather than silently creating a new empty one in the wrong place.
if ! restic cat config >/dev/null 2>&1; then
  echo "cannot open the restic repository at $RESTIC_REPOSITORY." >&2
  echo "Wrong address or password? For a brand-new repository run once: restic init" >&2
  exit 2
fi

echo "== local copies"
for client_dir in "$CLIENTS_DIR"/*/; do
  client_dir="${client_dir%/}"
  client="$(basename "$client_dir")"
  database="$client_dir/data/lead_bot.db"
  if [ ! -f "$database" ]; then
    # A client folder with no database means it never started or the folder is wrong. Either way
    # a quiet skip would hide a client that is not being backed up.
    fail "$client: no database at $database"
    continue
  fi
  mkdir -p "$BACKUP_DIR/$client"
  partial="$BACKUP_DIR/$client/lead_bot.db.partial"
  final="$BACKUP_DIR/$client/lead_bot.db"
  rm -f "$partial"
  if ! sqlite3 -bail "$database" ".timeout 10000" ".backup '$partial'" >/dev/null 2>"$partial.err"; then
    fail "$client: sqlite3 .backup failed: $(head -c 300 "$partial.err")"
    rm -f "$partial" "$partial.err"
    continue
  fi
  rm -f "$partial.err"
  integrity="$(sqlite3 "$partial" "PRAGMA integrity_check" 2>&1 | head -n 1)"
  if [ "$integrity" != "ok" ]; then
    fail "$client: the copy failed its integrity check ($integrity); yesterday's copy was kept"
    rm -f "$partial"
    continue
  fi
  # Only now replace yesterday's copy. A rename is atomic, so there is never a moment when the
  # only local copy is half written.
  mv -f "$partial" "$final"
  echo "ok      $client: $(stat -c %s "$final") bytes, integrity ok"
  succeeded=$((succeeded + 1))
done

echo "== off-server copy"
if [ "$succeeded" -eq 0 ]; then
  fail "no client copy succeeded, so nothing was uploaded"
else
  # Uploading what exists even when one client failed: the healthy clients still get their
  # backup tonight. The failure is in the exit code.
  if restic backup --host "$BACKUP_HOST" --tag nightly --quiet "$BACKUP_DIR"; then
    echo "ok      uploaded $BACKUP_DIR"
  else
    fail "restic backup failed"
  fi
  if restic forget --host "$BACKUP_HOST" --keep-daily "$KEEP_DAILY" --keep-monthly "$KEEP_MONTHLY" --prune --quiet; then
    echo "ok      thinned old snapshots (keep $KEEP_DAILY daily, $KEEP_MONTHLY monthly)"
  else
    fail "restic forget/prune failed"
  fi
fi

echo "== summary: $succeeded client(s) backed up, $failed failure(s)"
[ "$failed" -eq 0 ]
