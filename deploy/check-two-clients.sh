#!/usr/bin/env bash
# lead-bot/deploy/check-two-clients.sh
# The deployment step 3 check, runnable any time on a machine with Docker: builds the image, starts
# Caddy and TWO fake clients (alpha, bravo) from the real templates, and proves they work side by
# side. Uses ports 18080/18443 and throwaway folders, and removes everything it made on exit.
#
#   bash deploy/check-two-clients.sh
#
# Nothing here is real: placeholder model keys, a fictional 555-01xx phone number, the example
# client config, and worker dry runs (texts are printed, never sent).
set -u

REPO="$(cd "$(dirname "$0")/.." && { pwd -W 2>/dev/null || pwd; })"   # C:/... on Windows: native tools need it
WORK="${CHECK_DIR:-${TMPDIR:-/tmp}/lead-bot-two-clients}"
HTTP_PORT=18080
IMAGE_TAG=check
FAILURES=0

# Git-bash on Windows rewrites paths in arguments to docker; turn that off, and give compose
# Windows-style paths (C:/...) which it understands on both systems.
export MSYS_NO_PATHCONV=1
to_docker_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }

pass() { echo "  ok    $1"; }
fail() { echo "  FAIL  $1"; FAILURES=$((FAILURES + 1)); }
check() { # check "description" command...
  local description="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$description"; else fail "$description"; fi
}

cleanup() {
  if [ "${CHECK_KEEP:-}" = 1 ]; then echo "== CHECK_KEEP=1: leaving everything running under $WORK"; return; fi
  echo "== cleanup"
  for client in alpha bravo; do
    CLIENT=$client IMAGE_TAG=$IMAGE_TAG CLIENT_DIR="$(to_docker_path "$WORK/clients/$client")" WORKER_FLAGS="" \
      docker compose -p "lead-bot-$client" -f "$REPO/deploy/compose.template.yaml" down -v >/dev/null 2>&1
  done
  CADDY_DIR="$(to_docker_path "$WORK/caddy")" docker compose -p lead-bot-caddy -f "$REPO/deploy/caddy/compose.yaml" down -v >/dev/null 2>&1
  docker rmi "lead-bot:$IMAGE_TAG" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

through_caddy() { # through_caddy <client> <curl args...>: reach a client by its host name via Caddy
  local client="$1"; shift
  curl -s -H "Host: $client.localhost" "$@"
}
container_of() { # container_of <client> <service>
  docker ps -q --filter "label=com.docker.compose.project=lead-bot-$1" --filter "label=com.docker.compose.service=$2"
}
sql() { # sql <client> "<query>": run a read-only query in that client's own database
  docker exec "$(container_of "$1" server)" python -c "
import sqlite3, sys
connection = sqlite3.connect('file:/data/lead_bot.db?mode=ro', uri=True)
print(connection.execute(sys.argv[1]).fetchone()[0])" "$2"
}

echo "== build the image"
docker build -q -t "lead-bot:$IMAGE_TAG" "$REPO" >/dev/null || { echo "build failed"; exit 1; }

echo "== lay out two fake clients and Caddy under $WORK"
rm -rf "$WORK"; mkdir -p "$WORK/caddy/sites"
PHONE="480""555""0100"   # 555-01xx is reserved for fiction
# Texts the bot starts only go out 8:00-21:00 local time, so a check run at night would see the
# worker (correctly) hold the first text back. Give the fake clients a fixed-offset timezone in
# which it is 14:00 right now. Etc/GMT-2 means UTC+2: the sign in the name is inverted.
utc_hour=$((10#$(date -u +%H)))
offset=$(( (14 - utc_hour + 36) % 24 - 12 ))   # whole hours from UTC, in -12..+11
if [ "$offset" -eq 0 ]; then TIMEZONE="Etc/GMT"; elif [ "$offset" -gt 0 ]; then TIMEZONE="Etc/GMT-$offset"; else TIMEZONE="Etc/GMT+$((-offset))"; fi
for client in alpha bravo; do
  directory="$WORK/clients/$client"
  mkdir -p "$directory/data"
  cp "$REPO/client.example.toml" "$directory/client.toml"
  cat > "$directory/.env" <<EOF
BUSINESS_NAME=${client^} Roofing LLC
BUSINESS_TIMEZONE=$TIMEZONE
BUSINESS_CALLBACK_PHONE=$PHONE
PUBLIC_BASE_URL=https://$client.bot.example.com
TYPESAFE_API_KEY=placeholder
OPENAI_API_KEY=placeholder
EOF
  sed -e "s/@CLIENT@/$client/g" -e "s#@SITE_ADDRESS@#http://$client.localhost:80#g" \
    "$REPO/deploy/Caddyfile.template" > "$WORK/caddy/sites/$client.caddy"
done
cp "$REPO/deploy/caddy/Caddyfile" "$WORK/caddy/Caddyfile"
# The data folders must be writable by the image's user (UID 1000). On Docker Desktop for
# Windows a bind mount is open to everyone already; on the VPS this is a chown.

echo "== a missing variable is refused, not defaulted"
if CLIENT=alpha IMAGE_TAG=$IMAGE_TAG CLIENT_DIR="$(to_docker_path "$WORK/clients/alpha")" \
  docker compose -p lead-bot-alpha -f "$REPO/deploy/compose.template.yaml" config >/dev/null 2>&1; then
  fail "compose accepted a run with WORKER_FLAGS unset (a forgotten --send must not be guessed)"
else
  pass "compose refuses to start with WORKER_FLAGS unset"
fi

echo "== start Caddy and both clients"
CADDY_HTTP_PORT=$HTTP_PORT CADDY_HTTPS_PORT=18443 CADDY_DIR="$(to_docker_path "$WORK/caddy")" \
  docker compose -p lead-bot-caddy -f "$REPO/deploy/caddy/compose.yaml" up -d >/dev/null 2>&1 || fail "caddy did not start"
for client in alpha bravo; do
  CLIENT=$client IMAGE_TAG=$IMAGE_TAG CLIENT_DIR="$(to_docker_path "$WORK/clients/$client")" WORKER_FLAGS="" \
    docker compose -p "lead-bot-$client" -f "$REPO/deploy/compose.template.yaml" up -d >/dev/null 2>&1 \
    || fail "$client did not start"
done

echo "== /health through Caddy, for both clients"
for client in alpha bravo; do
  status=000
  for _ in $(seq 1 45); do
    status=$(through_caddy "$client" -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT/health")
    [ "$status" = 200 ] && break
    sleep 2
  done
  [ "$status" = 200 ] && pass "$client /health 200 (server up and its worker beating)" || fail "$client /health = $status"
done

echo "== a form posted to alpha lands only in alpha"
consent_version=$(cd "$REPO" && uv run python -c "from lead_bot.consent import CONSENT_VERSION; print(CONSENT_VERSION)")
post_status=$(through_caddy alpha -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$HTTP_PORT/leads" \
  --data-urlencode "full_name=Dana Ruiz" --data-urlencode "phone=(602) 555-""0142" \
  --data-urlencode "address=12 Saguaro Ln, Mesa AZ" --data-urlencode "problem=active_leak" \
  --data-urlencode "text_consent=yes" --data-urlencode "consent_version=$consent_version")
[ "$post_status" = 200 ] && pass "form accepted (200)" || fail "form post = $post_status"
[ "$(sql alpha 'SELECT COUNT(*) FROM leads')" = 1 ] && pass "alpha's database has the lead" || fail "alpha's lead count is not 1"
[ "$(sql bravo 'SELECT COUNT(*) FROM leads')" = 0 ] && pass "bravo's database has none" || fail "bravo's lead count is not 0"

echo "== consent evidence names the public page and keeps the visitor's address"
[ "$(sql alpha 'SELECT source FROM consents')" = "https://alpha.bot.example.com/leads" ] \
  && pass "source is the public URL, not lead-bot-alpha-server:8000" || fail "consent source is $(sql alpha 'SELECT source FROM consents')"
[ "$(sql alpha "SELECT COALESCE(forwarded_for, '') != '' FROM consents")" = 1 ] \
  && pass "X-Forwarded-For from Caddy was recorded" || fail "no X-Forwarded-For recorded"

echo "== alpha's worker picks the lead up (dry run prints the first text)"
picked_up=no
for _ in $(seq 1 30); do
  if docker logs "$(container_of alpha worker)" 2>&1 | grep -q "text to"; then picked_up=yes; break; fi
  sleep 2
done
[ "$picked_up" = yes ] && pass "alpha's worker printed the opening text" \
  || fail "no opening text in alpha's worker log (see the timezone note above: texts start only 8:00-21:00 local)"
docker logs "$(container_of bravo worker)" 2>&1 | grep -q "text to" \
  && fail "bravo's worker printed a text it should not have" || pass "bravo's worker printed nothing"

echo "== a WebSocket reaches /calls/relay through Caddy"
(cd "$REPO" && uv run python - "$HTTP_PORT" <<'PY'
import asyncio, json, sys
from websockets.asyncio.client import connect

async def main(port: str) -> None:
    # Connect to the local port but present alpha's host name, so Caddy picks alpha's site.
    async with connect(f"ws://alpha.localhost:{port}/calls/relay", host="127.0.0.1", port=int(port)) as socket:
        await socket.send(json.dumps({"type": "not-a-setup-message"}))
        try:
            await asyncio.wait_for(socket.recv(), 5)
        except Exception:
            pass
        await socket.wait_closed()
    print("handshake ok, closed by the app")

asyncio.run(main(sys.argv[1]))
PY
) && pass "WebSocket upgraded through Caddy and the app closed it" || fail "WebSocket through Caddy failed"
docker logs "$(container_of alpha server)" 2>&1 | grep -q "relay refused: the first message wasn't a setup message" \
  && pass "alpha's app saw the bad setup message (the socket really reached it)" || fail "alpha's log shows no relay refusal"
docker logs "$(container_of bravo server)" 2>&1 | grep -q "relay refused" \
  && fail "bravo's app saw alpha's socket" || pass "bravo's app saw nothing"

echo "== an unknown host name gets nothing"
unknown=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: nobody.localhost" "http://127.0.0.1:$HTTP_PORT/health")
[ "$unknown" = 404 ] && pass "unknown host -> 404" || fail "unknown host -> $unknown (want 404)"

echo "== neither client container publishes a port to the host"
docker ps --format '{{.Names}} {{.Ports}}' | grep -E "^lead-bot-(alpha|bravo)-" | grep -q '0.0.0.0' \
  && fail "a client container publishes a host port" || pass "only Caddy has published ports"

echo
if [ "$FAILURES" = 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILURES CHECK(S) FAILED"; fi
exit "$FAILURES"
