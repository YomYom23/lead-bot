#!/usr/bin/env bash
# lead-bot/deploy/check-update.sh
# The deployment step 10 check: builds images from a commit, creates a client, starts it, updates it
# to a new tag, rolls it back (timed), survives a bad update, and shows no data is lost on the way.
# Uses Caddy on port 18080 and throwaway folders and images; removes everything it made on exit.
#
#   bash deploy/check-update.sh
#
# Nothing real is used: placeholder keys, a fictional 555-01xx number, worker dry runs.
set -u

REPO="$(cd "$(dirname "$0")/.." && { pwd -W 2>/dev/null || pwd; })"
export MSYS_NO_PATHCONV=1
to_docker_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
WORK="$(to_docker_path "${TMPDIR:-/tmp}")/lead-bot-update-check"
ROOT="$WORK/root"
export LEAD_BOT_ROOT="$ROOT"
HTTP_PORT=18080
V2=lead-bot:check-v2
BROKEN=lead-bot:check-broken
FAILURES=0

pass() { echo "  ok    $1"; }
fail() { echo "  FAIL  $1"; FAILURES=$((FAILURES + 1)); }
expect_equal() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (got '$2', wanted '$3')"; fi; }
expect_in() { if echo "$2" | grep -qF -- "$3"; then pass "$1"; else fail "$1 (no '$3' in: $(echo "$2" | tail -n 3 | tr '\n' ' '))"; fi; }

SHA_TAG=""
cleanup() {
  [ "${CHECK_KEEP:-}" = 1 ] && { echo "== CHECK_KEEP=1: left running under $WORK"; return; }
  if [ -f "$ROOT/clients/demo/deploy.env" ]; then
    IMAGE_TAG=x docker compose --env-file "$ROOT/clients/demo/deploy.env" -p lead-bot-demo -f "$REPO/deploy/compose.template.yaml" down -v >/dev/null 2>&1
  fi
  CADDY_DIR="$ROOT/caddy" docker compose -p lead-bot-caddy -f "$REPO/deploy/caddy/compose.yaml" down -v >/dev/null 2>&1
  docker rmi "$V2" "$BROKEN" ${SHA_TAG:+"lead-bot:$SHA_TAG"} >/dev/null 2>&1
  rm -f "$REPO/src/lead_bot/_untracked_marker.py"
  rm -rf "$WORK"
}
trap cleanup EXIT
rm -rf "$WORK"; mkdir -p "$ROOT/caddy/sites"

server_container() { docker ps -q --filter "label=com.docker.compose.project=lead-bot-demo" --filter "label=com.docker.compose.service=server" | head -n 1; }
server_image() { docker inspect --format '{{.Config.Image}}' "$(server_container)" 2>/dev/null; }
server_image_id() { docker inspect --format '{{.Image}}' "$(server_container)" 2>/dev/null; }
health_through_caddy() { curl -s -o /dev/null -w '%{http_code}' -H "Host: demo.localhost" "http://127.0.0.1:$HTTP_PORT/health"; }
live_leads() { docker exec "$(server_container)" python -c "import sqlite3; print(sqlite3.connect('file:/data/lead_bot.db?mode=ro', uri=True).execute('SELECT COUNT(*) FROM leads').fetchone()[0])" 2>/dev/null; }
deploy_setting() { grep -E "^$1=" "$ROOT/clients/demo/deploy.env" | cut -d= -f2-; }
update() { bash "$REPO/deploy/update.sh" "$@" 2>&1; }

echo "== build-image.sh: an image is exactly one commit"
echo "# not committed" > "$REPO/src/lead_bot/_untracked_marker.py"
output=$(bash "$REPO/deploy/build-image.sh" 2>&1); status=$?
rm -f "$REPO/src/lead_bot/_untracked_marker.py"
SHA_TAG=$(echo "$output" | tail -n 1)
expect_equal "build-image.sh succeeds" "$status" 0
expect_equal "the tag is the commit's short sha" "$SHA_TAG" "$(git -C "$REPO" rev-parse --short=10 HEAD)"
V1="lead-bot:$SHA_TAG"
expect_equal "an untracked file in the working folder is not in the image" "$(docker run --rm "$V1" python -c "import lead_bot, os; print(os.path.exists(os.path.join(os.path.dirname(lead_bot.__file__), '_untracked_marker.py')))")" False
expect_equal "an unknown git ref is refused" "$(bash "$REPO/deploy/build-image.sh" no-such-ref >/dev/null 2>&1; echo $?)" 1
# v2: the same code with a different label, so it is a different image. broken: starts, never serves.
git -C "$REPO" archive --format=tar HEAD | docker build -q --label check.version=2 -t "$V2" - >/dev/null
printf 'FROM %s\nCMD ["python","-c","import time; time.sleep(3600)"]\n' "$V2" | docker build -q -t "$BROKEN" - >/dev/null
[ "$(docker inspect --format '{{.Id}}' "$V1")" != "$(docker inspect --format '{{.Id}}' "$V2")" ] && pass "v1 and v2 are different images" || fail "v1 and v2 are the same image; the update test would prove nothing"

echo "== start Caddy (no clients yet)"
cp "$REPO/deploy/caddy/Caddyfile" "$ROOT/caddy/Caddyfile"
CADDY_HTTP_PORT=$HTTP_PORT CADDY_HTTPS_PORT=18443 CADDY_DIR="$ROOT/caddy" docker compose -p lead-bot-caddy -f "$REPO/deploy/caddy/compose.yaml" up -d >/dev/null 2>&1 \
  && pass "caddy started with an empty sites folder" || fail "caddy did not start"
sleep 2

echo "== new-client.sh"
for bad in "Bad_Name" "-dash" "has space" ""; do
  bash "$REPO/deploy/new-client.sh" "$bad" demo.bot.example.com >/dev/null 2>&1; code=$?
  [ "$code" = 2 ] && pass "client name '$bad' is refused" || fail "client name '$bad' gave exit $code (wanted 2)"
done
bash "$REPO/deploy/new-client.sh" demo "not a host" >/dev/null 2>&1; [ "$?" = 2 ] && pass "a bad host name is refused" || fail "a bad host name was accepted"
expect_equal "refusals created nothing" "$(ls "$ROOT/clients" 2>/dev/null | wc -l)" 0
output=$(LEAD_BOT_SITE_ADDRESS="http://demo.localhost:80" LEAD_BOT_PUBLIC_BASE_URL="https://demo.bot.example.com" bash "$REPO/deploy/new-client.sh" demo demo.bot.example.com 2>&1); status=$?
echo "$output" | sed 's/^/   | /'
expect_equal "new-client.sh succeeds" "$status" 0
expect_in "caddy was reloaded" "$output" "caddy: reloaded"
for file in .env client.toml deploy.env; do [ -f "$ROOT/clients/demo/$file" ] && pass "$file created" || fail "$file missing"; done
[ -d "$ROOT/clients/demo/data" ] && pass "data/ created" || fail "data/ missing"
expect_in ".env still has CHANGE_ME lines to fill in" "$(cat "$ROOT/clients/demo/.env")" "CHANGE_ME"
expect_in "PUBLIC_BASE_URL was filled in" "$(cat "$ROOT/clients/demo/.env")" "PUBLIC_BASE_URL=https://demo.bot.example.com"
expect_equal "a new client is a dry-run client (WORKER_FLAGS empty)" "$(deploy_setting WORKER_FLAGS)" ""
expect_equal "and has no image tag yet" "$(deploy_setting IMAGE_TAG)" ""
touch "$WORK/mode-probe"; chmod 600 "$WORK/mode-probe"
if [ "$(stat -c %a "$WORK/mode-probe")" = 600 ]; then
  expect_equal ".env is owner-only (600)" "$(stat -c %a "$ROOT/clients/demo/.env")" 600
else
  echo "  skip  .env mode: this filesystem doesn't keep file modes (the VPS's does; new-client.sh sets umask 077 and chmod 600)"
fi
before=$(find "$ROOT" -type f | sort | xargs sha256sum | sha256sum)
bash "$REPO/deploy/new-client.sh" demo demo.bot.example.com >/dev/null 2>&1; code=$?
expect_equal "creating the same client twice is refused" "$code" 1
expect_equal "and changes nothing" "$(find "$ROOT" -type f | sort | xargs sha256sum | sha256sum)" "$before"
LEAD_BOT_SITE_ADDRESS="bad {{ address" bash "$REPO/deploy/new-client.sh" worse x.bot.example.com >/dev/null 2>&1; code=$?
expect_equal "a site address Caddy rejects is refused" "$code" 1
expect_equal "and leaves no folder or site file behind" "$(ls "$ROOT/clients/worse" "$ROOT/caddy/sites/worse.caddy" 2>/dev/null | wc -l)" 0
expect_equal "and Caddy still serves the other clients" "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: nobody.localhost' "http://127.0.0.1:$HTTP_PORT/")" 404

echo "== update.sh refuses before touching anything"
output=$(update demo "$SHA_TAG"); status=$?
expect_equal "refused while .env has CHANGE_ME" "$status" 1
expect_in "and says why" "$output" "CHANGE_ME"
# Fill the .env with test values (no real keys).
PHONE="480""555""0100"
cat > "$ROOT/clients/demo/.env" <<EOF
BUSINESS_NAME=Demo Roofing LLC
BUSINESS_CALLBACK_PHONE=$PHONE
PUBLIC_BASE_URL=https://demo.bot.example.com
TYPESAFE_API_KEY=placeholder
OPENAI_API_KEY=placeholder
EOF
output=$(update demo no-such-tag); status=$?
expect_equal "an image that doesn't exist is refused" "$status" 1
expect_in "with the way to build it" "$output" "build-image.sh"
expect_equal "and nothing was started" "$(server_container | wc -l)" 0
output=$(update demo 'x;rm -rf'); status=$?
expect_equal "an unsafe tag is refused" "$status" 1
expect_in "by the tag check itself, not by a later layer" "$output" "bad image tag"
expect_equal "--rollback with no previous tag is refused" "$(update demo --rollback >/dev/null; echo $?)" 1

echo "== first start on v1"
output=$(update demo "$SHA_TAG"); status=$?
echo "$output" | sed 's/^/   | /'
expect_equal "update.sh succeeds" "$status" 0
expect_equal "the server runs v1" "$(server_image)" "$V1"
expect_equal "deploy.env records v1" "$(deploy_setting IMAGE_TAG)" "$SHA_TAG"
expect_equal "/health is 200 through Caddy" "$(health_through_caddy)" 200
consent_version=$(cd "$REPO" && uv run python -c "from lead_bot.consent import CONSENT_VERSION; print(CONSENT_VERSION)")
post=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: demo.localhost" -X POST "http://127.0.0.1:$HTTP_PORT/leads" \
  --data-urlencode "full_name=Dana Ruiz" --data-urlencode "phone=(602) 555-""0142" --data-urlencode "address=12 Saguaro Ln, Mesa AZ" \
  --data-urlencode "problem=active_leak" --data-urlencode "text_consent=yes" --data-urlencode "consent_version=$consent_version")
expect_equal "a lead is accepted before the update" "$post" 200
expect_equal "it is in the database" "$(live_leads)" 1
output=$(update demo "$SHA_TAG"); expect_in "running update.sh again with the same tag does nothing" "$output" "already healthy"

echo "== --restart: a changed .env needs new containers even on the same image"
echo "CHECK_RESTART_MARKER=yes" >> "$ROOT/clients/demo/.env"
container_before=$(server_container)
expect_equal "before the restart the container does not know the new setting" "$(docker exec "$container_before" printenv CHECK_RESTART_MARKER 2>/dev/null)" ""
output=$(update demo --restart); status=$?
echo "$output" | sed 's/^/   | /'
expect_equal "restart succeeds" "$status" 0
[ "$(server_container)" != "$container_before" ] && pass "the server container was replaced" || fail "the server container is the same one"
expect_equal "and it sees the changed .env" "$(docker exec "$(server_container)" printenv CHECK_RESTART_MARKER 2>/dev/null)" yes
expect_equal "still on the same image" "$(server_image)" "$V1"
expect_equal "the lead survived the restart" "$(live_leads)" 1
expect_equal "deploy.env is unchanged by a restart" "$(deploy_setting IMAGE_TAG)/$(deploy_setting PREVIOUS_IMAGE_TAG)" "$SHA_TAG/"
# Compose recreates containers by itself when .env changed, so the case above does not need
# --force-recreate. A restart with NOTHING changed does: that is what makes --restart a restart.
container_before=$(server_container)
output=$(update demo --restart); status=$?
expect_equal "a restart with nothing changed succeeds" "$status" 0
[ "$(server_container)" != "$container_before" ] && pass "and still replaces the containers" || fail "a restart with nothing changed left the same containers running"

echo "== update to v2"
v1_id=$(server_image_id)
output=$(update demo check-v2); status=$?
echo "$output" | sed 's/^/   | /'
expect_equal "update to v2 succeeds" "$status" 0
expect_equal "the server now runs v2" "$(server_image)" "$V2"
[ "$(server_image_id)" != "$v1_id" ] && pass "and it is a different image from v1" || fail "image id unchanged"
expect_equal "deploy.env: IMAGE_TAG is v2" "$(deploy_setting IMAGE_TAG)" "check-v2"
expect_equal "deploy.env: PREVIOUS_IMAGE_TAG is v1" "$(deploy_setting PREVIOUS_IMAGE_TAG)" "$SHA_TAG"
expect_equal "the lead survived the update" "$(live_leads)" 1
expect_equal "/health is 200 through Caddy" "$(health_through_caddy)" 200

echo "== roll back (timed)"
started=$SECONDS
output=$(update demo --rollback); status=$?
elapsed=$((SECONDS - started))
echo "$output" | sed 's/^/   | /'
expect_equal "rollback succeeds" "$status" 0
expect_equal "the server runs v1 again" "$(server_image)" "$V1"
[ "$elapsed" -lt 60 ] && pass "rollback took $elapsed s (under a minute)" || fail "rollback took $elapsed s"
expect_equal "the lead survived the rollback" "$(live_leads)" 1
expect_equal "/health is 200 through Caddy" "$(health_through_caddy)" 200
expect_equal "and back to v2 with another --rollback" "$(update demo --rollback >/dev/null; server_image)" "$V2"

echo "== a bad update rolls itself back"
output=$(HEALTH_TIMEOUT=20 update demo check-broken); status=$?
echo "$output" | sed 's/^/   | /'
expect_equal "the bad update exits 1" "$status" 1
expect_in "it says it rolled back" "$output" "rolling back to lead-bot:check-v2"
expect_equal "the server is back on v2" "$(server_image)" "$V2"
expect_equal "deploy.env still says v2, not the bad tag" "$(deploy_setting IMAGE_TAG)" "check-v2"
expect_equal "/health is 200 through Caddy" "$(health_through_caddy)" 200
expect_equal "the lead is still there" "$(live_leads)" 1

echo
if [ "$FAILURES" = 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILURES CHECK(S) FAILED"; fi
exit "$FAILURES"
