#!/usr/bin/env bash
# lead-bot/deploy/check-backup.sh
# The deployment step 4 check: backs up real lead-bot databases the way the VPS will, restores
# them, and proves the restored data is complete and intact. Runs backup.sh and restore-check.sh
# inside ubuntu:26.04 (the VPS's OS, with its apt versions of sqlite3 and restic) as UID 1000,
# against a Docker volume that stands in for /srv/lead-bot. The restic repository is a second
# volume standing in for off-server storage. Everything it makes is removed on exit.
#
#   bash deploy/check-backup.sh
#
# Fake clients, each there to prove one thing:
#   alpha    a live writer adds leads while the backup runs   -> the restore is one real moment
#   bravo    another process keeps the database open, so its  -> a plain `cp` of the .db would lose
#            newest writes sit in the -wal file                  them; .backup must not
#   delta    healthy, backed up, then corrupted on disk       -> failure reported, yesterday's
#                                                                good copy kept
#   charlie  a client folder with no database                 -> reported, not skipped quietly
# Nothing real is used: fictional 555-01xx phone numbers and a throwaway restic password.
set -u

REPO="$(cd "$(dirname "$0")/.." && { pwd -W 2>/dev/null || pwd; })"
export MSYS_NO_PATHCONV=1
to_docker_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
WORK="${TMPDIR:-/tmp}/lead-bot-backup-check"
DATA_VOLUME=leadbot-bk-data
REPO_VOLUME=leadbot-bk-repo
BOT_IMAGE=lead-bot:bkcheck
RUNNER_BASE="${RUNNER_BASE:-ubuntu:26.04}"   # the VPS's OS (checked on 2026-10-11); ubuntu:24.04 also passes
RUNNER_IMAGE="lead-bot-backup-check:${RUNNER_BASE//[:\/]/-}"
FAILURES=0

pass() { echo "  ok    $1"; }
fail() { echo "  FAIL  $1"; FAILURES=$((FAILURES + 1)); }
expect_equal() { # expect_equal "description" actual wanted
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (got '$2', wanted '$3')"; fi
}
expect_in() { # expect_in "description" "text" "needle"
  if echo "$2" | grep -qF -- "$3"; then pass "$1"; else fail "$1 (no '$3' in the output)"; fi
}

remove_test_objects() {
  docker rm -f leadbot-bk-holder leadbot-bk-writer >/dev/null 2>&1
  docker volume rm "$DATA_VOLUME" "$REPO_VOLUME" >/dev/null 2>&1
}
cleanup() {
  remove_test_objects
  rm -rf "$WORK"
  if [ "${CHECK_KEEP_IMAGES:-}" != 1 ]; then docker rmi "$BOT_IMAGE" "$RUNNER_IMAGE" >/dev/null 2>&1; fi
}
trap cleanup EXIT
remove_test_objects   # leftovers from an interrupted earlier run
rm -rf "$WORK"; mkdir -p "$WORK"

# Run a command as the data's owner (UID 1000) in the VPS-like container.
runner() {
  docker run --rm --user 1000 \
    -v "$DATA_VOLUME":/srv/lead-bot -v "$REPO_VOLUME":/repo -v "$(to_docker_path "$REPO/deploy")":/deploy:ro \
    -e RESTIC_REPOSITORY="${REPOSITORY_ADDRESS:-/repo}" -e RESTIC_PASSWORD=check-only-not-a-secret \
    -e RESTIC_CACHE_DIR=/tmp/restic-cache -e HOME=/tmp \
    "$RUNNER_IMAGE" "$@"
}
database_of() { echo "/srv/lead-bot/clients/$1/data/lead_bot.db"; }
# Run the fill script in the bot's own image (as its user, UID 1000): real databases, real Store.
fill() { # fill <client> <first> <last> <delay_seconds_between_leads>
  docker run --rm --user 1000 -v "$DATA_VOLUME":/srv/lead-bot -v "$(to_docker_path "$WORK")":/work:ro "$BOT_IMAGE" \
    python /work/fill.py "$(database_of "$1")" "$2" "$3" "$4"
}
count_live() { runner sqlite3 "$(database_of "$1")" "SELECT COUNT(*) FROM leads"; }
leads_in() { echo "$1" | sed -n 's/^ *leads: //p'; }   # the number from restore-check's output

cat > "$WORK/fill.py" <<'PY'
# Adds leads 'first' to 'last'-1 to a lead-bot database through the real Store, one form
# submission each, with consent evidence. With first == last it only creates the schema.
import sys, time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from lead_bot.consent import CONSENT_VERSION, ConsentEvidence, consent_text
from lead_bot.leads import parse_lead_form
from lead_bot.store import Store
from lead_bot.timing import TextingHours

database_path, first, last, delay = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
store = Store(database_path)
hours = TextingHours(ZoneInfo("America/Phoenix"))
for number in range(first, last):
    # 555-0100 to 555-0199 are the numbers reserved for fiction.
    lead = parse_lead_form(
        f"Test Person {number}", f"(480) 555-{100 + number:04d}", f"{number} Saguaro Ln, Mesa AZ", "active_leak"
    )
    consent = ConsentEvidence(
        given=True, version=CONSENT_VERSION, text_shown=consent_text("Check Roofing LLC"),
        ip_address="203.0.113.7", forwarded_for=None, user_agent="check", source="https://check.example.com/leads",
    )
    store.add_lead(lead, consent, datetime.now(UTC), hours)
    time.sleep(delay)
PY

echo "== build the bot image and the VPS-like runner ($RUNNER_BASE + apt sqlite3 and restic)"
docker build -q -t "$BOT_IMAGE" "$REPO" >/dev/null || { echo "bot image build failed"; exit 1; }
if ! docker image inspect "$RUNNER_IMAGE" >/dev/null 2>&1; then
  printf 'FROM %s\nRUN apt-get update -qq && apt-get install -y -qq sqlite3 restic ca-certificates >/dev/null && rm -rf /var/lib/apt/lists/*\n' "$RUNNER_BASE" \
    | docker build -q -t "$RUNNER_IMAGE" - >/dev/null || { echo "runner image build failed"; exit 1; }
fi
runner sh -c 'echo "   runner: sqlite3 $(sqlite3 --version | cut -d" " -f1), $(restic version | cut -d" " -f1,2)"'

echo "== lay out the clients and the stand-in off-server repository"
docker volume create "$DATA_VOLUME" >/dev/null; docker volume create "$REPO_VOLUME" >/dev/null
# A new volume is owned by root; hand it to UID 1000, as VPS prep (step 5) will do for the real one.
docker run --rm --user root -v "$DATA_VOLUME":/srv/lead-bot -v "$REPO_VOLUME":/repo "$BOT_IMAGE" \
  sh -c 'for c in alpha bravo delta; do mkdir -p /srv/lead-bot/clients/$c/data; done; chown -R 1000:1000 /srv/lead-bot /repo'
# Each client's keys and facts. The sentinel text stands in for a real secret, so the check can
# prove it comes back intact and never appears readable in the off-server repository.
docker run --rm --user root -v "$DATA_VOLUME":/srv/lead-bot "$BOT_IMAGE" sh -c '
  for c in alpha bravo delta; do
    echo "TWILIO_AUTH_TOKEN=LEAD_BOT_CHECK_SECRET_SENTINEL_$c" > /srv/lead-bot/clients/$c/.env
    echo "[business]  # LEAD_BOT_CHECK_FACTS_SENTINEL_$c" > /srv/lead-bot/clients/$c/client.toml
    chown 1000:1000 /srv/lead-bot/clients/$c/.env /srv/lead-bot/clients/$c/client.toml; chmod 600 /srv/lead-bot/clients/$c/.env
  done'
for client in alpha bravo; do fill "$client" 0 0 0 >/dev/null; done   # schema only (this puts the file in WAL mode)
fill alpha 0 10 0 >/dev/null
fill delta 0 30 0 >/dev/null
runner restic init --quiet >/dev/null 2>&1 && pass "restic repository created" || fail "restic init failed"

echo "== bravo: another process holds the database open while 25 leads arrive"
docker run -d --name leadbot-bk-holder --user 1000 -v "$DATA_VOLUME":/srv/lead-bot "$BOT_IMAGE" python -c "
import sqlite3, time
connection = sqlite3.connect('$(database_of bravo)')
connection.execute('SELECT COUNT(*) FROM leads').fetchone()
time.sleep(900)" >/dev/null
sleep 2
fill bravo 0 25 0 >/dev/null
wal_bytes=$(runner sh -c "stat -c %s $(database_of bravo)-wal 2>/dev/null || echo 0")
[ "$wal_bytes" -gt 0 ] && pass "bravo has $wal_bytes bytes waiting in its -wal file" || fail "bravo's -wal file is empty: the fixture can't show the WAL problem"
plain_copy_leads=$(runner sh -c "cp $(database_of bravo) /tmp/plain-copy.db && sqlite3 /tmp/plain-copy.db 'SELECT COUNT(*) FROM leads'")
[ "$plain_copy_leads" -lt 25 ] && pass "negative control: a plain cp of bravo's .db holds $plain_copy_leads of its 25 leads" \
  || fail "negative control: a plain cp already holds all 25 leads, so this fixture can't tell cp from .backup"

echo "== backup #1, while a live writer adds 60 leads to alpha"
docker run -d --name leadbot-bk-writer --user 1000 -v "$DATA_VOLUME":/srv/lead-bot -v "$(to_docker_path "$WORK")":/work:ro "$BOT_IMAGE" \
  python /work/fill.py "$(database_of alpha)" 10 70 0.15 >/dev/null
sleep 2
alpha_before=$(count_live alpha)
output=$(runner bash /deploy/backup.sh 2>&1); status=$?
echo "$output" | sed 's/^/   | /'
docker wait leadbot-bk-writer >/dev/null
alpha_after=$(count_live alpha)
expect_equal "backup exits 0" "$status" 0
expect_in "all three clients backed up" "$output" "3 client(s) backed up, 0 failure(s)"
expect_equal "local copies are owner-only (mode 600)" "$(runner stat -c %a /srv/lead-bot/backups/alpha/lead_bot.db)" 600

echo "== restore from the off-server repository into scratch folders"
restored_alpha=$(runner bash /deploy/restore-check.sh alpha 2>&1); echo "$restored_alpha" | sed 's/^/   | /'
restored_bravo=$(runner bash /deploy/restore-check.sh bravo 2>&1)
restored_delta=$(runner bash /deploy/restore-check.sh delta 2>&1)
alpha_restored=$(leads_in "$restored_alpha")
if [ -n "$alpha_restored" ] && [ "$alpha_restored" -ge "$alpha_before" ] && [ "$alpha_restored" -le "$alpha_after" ]; then
  pass "alpha restored $alpha_restored leads: between $alpha_before (before the backup) and $alpha_after (after), taken mid-write"
else
  fail "alpha restored '$alpha_restored' leads, outside $alpha_before..$alpha_after"
fi
expect_equal "alpha restore passes its integrity check" "$(echo "$restored_alpha" | grep -c 'integrity_check: ok')" 1
expect_equal "bravo restored every lead, including those only in the -wal file" "$(leads_in "$restored_bravo")" 25
expect_equal "delta restored all its leads" "$(leads_in "$restored_delta")" 30
expect_in "restore-check reports the config files came back" "$restored_bravo" "config files: .env and client.toml restored"
expect_equal "a restored .env is byte-identical to the live one" "$(runner sh -c 'restic restore latest --host lead-bot --target /tmp/cfg --include /srv/lead-bot/clients/bravo/.env --quiet && cmp /tmp/cfg/srv/lead-bot/clients/bravo/.env /srv/lead-bot/clients/bravo/.env && echo same' | tail -n 1)" same
expect_equal "no plaintext copy of the keys sits in the local backups folder" "$(runner find /srv/lead-bot/backups -name '.env*' -o -name 'client.toml' | wc -l)" 0
# The repository is on the volume as restic wrote it. Neither a key nor a lead's name may be readable in it.
expect_equal "the repository holds no readable key, facts or lead data" "$(docker run --rm -v "$REPO_VOLUME":/repo "$BOT_IMAGE" sh -c 'grep -rlF -e LEAD_BOT_CHECK_SECRET_SENTINEL -e LEAD_BOT_CHECK_FACTS_SENTINEL -e "Test Person" /repo | wc -l')" 0

echo "== backup #2: older snapshots are thinned (back-dated ones are injected with restic --time)"
# Two on the same past day, then one each in the previous two months. keep-daily/keep-monthly must
# drop the earlier of the same-day pair and keep the rest. (restic counts days and months that HAVE
# snapshots, not calendar time, so the result doesn't depend on when this check is run.) The same-day
# pair is taken with DIFFERENT path sets, as happens when a client is added between two nights:
# thinning must still treat them as one history.
config_paths=""
for c in alpha bravo delta; do config_paths="$config_paths /srv/lead-bot/clients/$c/.env /srv/lead-bot/clients/$c/client.toml"; done
runner sh -c "restic backup --host lead-bot --time '2026-10-05 02:00:00' --quiet /srv/lead-bot/backups
  for moment in '2026-10-05 03:00:00' '2026-09-30 02:00:00' '2026-08-20 02:00:00'; do
    restic backup --host lead-bot --time \"\$moment\" --quiet /srv/lead-bot/backups $config_paths; done"
runner bash /deploy/backup.sh >/dev/null 2>&1
snapshot_times=$(runner restic snapshots --host lead-bot --compact)
expect_in "the later snapshot of a past day is kept" "$snapshot_times" "2026-10-05 03:00:00"
expect_in "the previous month's is kept" "$snapshot_times" "2026-09-30 02:00:00"
expect_in "the month before that is kept" "$snapshot_times" "2026-08-20 02:00:00"
if echo "$snapshot_times" | grep -qF "2026-10-05 02:00:00"; then fail "the earlier snapshot of the same past day was not thinned"; else pass "the earlier snapshot of the same past day was thinned away"; fi

echo "== a corrupted database and a client with no database"
good_copy_hash=$(runner sha256sum /srv/lead-bot/backups/delta/lead_bot.db | cut -d' ' -f1)
docker rm -f leadbot-bk-holder >/dev/null 2>&1
leads_root_page=$(runner sqlite3 "$(database_of delta)" "SELECT rootpage FROM sqlite_master WHERE name='leads'")
# Overwrite the leads table's cell-pointer area with 0xFF: the file still opens, but is malformed.
runner sh -c "rm -f $(database_of delta)-wal $(database_of delta)-shm; head -c 64 /dev/zero | tr '\\0' '\\377' | dd of=$(database_of delta) bs=1 seek=\$((($leads_root_page - 1) * 4096 + 8)) conv=notrunc 2>/dev/null"
fixture_check=$(runner sqlite3 "$(database_of delta)" "PRAGMA integrity_check" 2>&1 | head -n 1)
[ "$fixture_check" != "ok" ] && pass "fixture: delta is now corrupt on disk ($fixture_check)" || fail "fixture: delta still passes integrity_check, the corruption test proves nothing"
runner mkdir -p /srv/lead-bot/clients/charlie/data
runner sh -c 'echo "TWILIO_AUTH_TOKEN=LEAD_BOT_CHECK_SECRET_SENTINEL_charlie" > /srv/lead-bot/clients/charlie/.env; echo "[business]" > /srv/lead-bot/clients/charlie/client.toml'
output=$(runner bash /deploy/backup.sh 2>&1); status=$?
echo "$output" | sed 's/^/   | /'
expect_equal "backup exits 1 when any client failed" "$status" 1
expect_in "the corrupt copy is reported" "$output" "FAILED  delta: the copy failed its integrity check"
expect_in "the missing database is reported" "$output" "FAILED  charlie: no database"
expect_in "healthy clients are still backed up" "$output" "2 client(s) backed up, 2 failure(s)"
expect_in "healthy clients are still uploaded" "$output" "ok      uploaded"
expect_equal "delta's last good local copy was kept untouched" "$(runner sha256sum /srv/lead-bot/backups/delta/lead_bot.db | cut -d' ' -f1)" "$good_copy_hash"
expect_equal "the kept copy is still intact" "$(runner sqlite3 /srv/lead-bot/backups/delta/lead_bot.db 'PRAGMA integrity_check')" ok
runner bash /deploy/restore-check.sh charlie >/dev/null 2>&1 && fail "restore-check passed for charlie, who has no backup" || pass "restore-check fails for a client with no backup"

echo "== restore-check refuses a corrupt snapshot"
# Put a corrupt copy into the repository by hand, as if a bad backup had got through.
runner sh -c "cp $(database_of delta) /srv/lead-bot/backups/delta/lead_bot.db && restic backup --host lead-bot --quiet /srv/lead-bot/backups /srv/lead-bot/clients/delta/.env /srv/lead-bot/clients/delta/client.toml"
output=$(runner bash /deploy/restore-check.sh delta 2>&1); status=$?
expect_equal "restore-check exits 1 on a corrupt snapshot" "$status" 1
expect_in "and says why" "$output" "failed its integrity check"

echo "== a client missing its client.toml is reported"
runner mv /srv/lead-bot/clients/bravo/client.toml /srv/lead-bot/clients/bravo/client.toml.hidden
output=$(runner bash /deploy/backup.sh 2>&1); status=$?
runner mv /srv/lead-bot/clients/bravo/client.toml.hidden /srv/lead-bot/clients/bravo/client.toml
expect_in "the missing client.toml is reported" "$output" "FAILED  bravo: no client.toml"
expect_equal "and the run exits 1" "$status" 1
# That run's snapshot is now the newest, and it has no client.toml for bravo: a restore must not pass.
output=$(runner bash /deploy/restore-check.sh bravo 2>&1); status=$?
expect_equal "restore-check exits 1 when the newest snapshot lacks the client's client.toml" "$status" 1
expect_in "and names the missing file" "$output" "client.toml is missing or empty in the snapshot"

echo "== a wrong repository stops the run before anything is written"
output=$(REPOSITORY_ADDRESS=/nonexistent runner bash /deploy/backup.sh 2>&1); status=$?
expect_equal "exit code 2 for an unreachable repository" "$status" 2
expect_in "with a message naming the problem" "$output" "cannot open the restic repository"

echo
if [ "$FAILURES" = 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILURES CHECK(S) FAILED"; fi
exit "$FAILURES"
