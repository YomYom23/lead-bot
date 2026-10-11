#!/usr/bin/env bash
# lead-bot/deploy/new-client.sh
# Sets up a new client's folder and web address. The start of the admin CLI (launch plan step 17).
#
#   bash deploy/new-client.sh <client> <host> [--send]
#
#   <client>  short name: lowercase letters, digits and dashes (acme, desert-ridge)
#   <host>    the public host name, e.g. acme.bot.example.com
#   --send    this client's worker sends real texts. Without it the client runs as a dry run that
#             only prints texts, so a half-configured client can never text anyone.
#
# Makes /srv/lead-bot/clients/<client>/ with a .env to fill in (mode 600), a client.toml to edit,
# an empty data/ folder, and deploy.env (which image tag the client runs, and whether it sends).
# Writes the client's Caddy site and reloads Caddy. It does NOT start the client: fill in .env and
# client.toml, then run `deploy/update.sh <client> <image tag>`.
#
# Settings (environment):
#   LEAD_BOT_ROOT              default /srv/lead-bot
#   LEAD_BOT_SITE_ADDRESS      Caddy site address; default <host>. Local tests use http://x.localhost:80
#   LEAD_BOT_PUBLIC_BASE_URL   default https://<host>
set -eu
umask 077   # the .env will hold keys: owner-only from the moment it exists

DEPLOY_DIR="$(cd "$(dirname "$0")" && { pwd -W 2>/dev/null || pwd; })"
ROOT="${LEAD_BOT_ROOT:-/srv/lead-bot}"
usage() { echo "usage: new-client.sh <client> <host> [--send]" >&2; exit 2; }

[ $# -ge 2 ] || usage
client="$1"; host="$2"; shift 2
worker_flags=""
for argument in "$@"; do
  case "$argument" in
    --send) worker_flags="--send" ;;
    *) usage ;;
  esac
done

# The name ends up in container names, a network alias and file paths, so keep it boring.
if ! printf '%s' "$client" | grep -Eq '^[a-z0-9][a-z0-9-]{0,30}$'; then
  echo "client name must be lowercase letters, digits and dashes (max 31): '$client'" >&2; exit 2
fi
if [ -z "${LEAD_BOT_SITE_ADDRESS:-}" ] && ! printf '%s' "$host" | grep -Eq '^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$'; then
  echo "host must be a lowercase domain name like acme.bot.example.com: '$host'" >&2; exit 2
fi

client_dir="$ROOT/clients/$client"
site_file="$ROOT/caddy/sites/$client.caddy"
if [ -e "$client_dir" ] || [ -e "$site_file" ]; then
  echo "client '$client' already exists ($client_dir): nothing was changed" >&2; exit 1
fi

site_address="${LEAD_BOT_SITE_ADDRESS:-$host}"
public_base_url="${LEAD_BOT_PUBLIC_BASE_URL:-https://$host}"

mkdir -p "$client_dir/data" "$ROOT/caddy/sites"
[ -f "$ROOT/caddy/Caddyfile" ] || cp "$DEPLOY_DIR/caddy/Caddyfile" "$ROOT/caddy/Caddyfile"

sed "s#@PUBLIC_BASE_URL@#$public_base_url#g" "$DEPLOY_DIR/client.env.template" > "$client_dir/.env"
chmod 600 "$client_dir/.env"
cp "$DEPLOY_DIR/../client.example.toml" "$client_dir/client.toml"

# deploy.env feeds `docker compose --env-file`. IMAGE_TAG stays empty until update.sh starts the
# client, so a client with no image can't be mistaken for a running one.
cat > "$client_dir/deploy.env" <<EOF
CLIENT=$client
CLIENT_DIR=$client_dir
IMAGE_TAG=
PREVIOUS_IMAGE_TAG=
WORKER_FLAGS=$worker_flags
EOF

# The data folder must belong to the user the containers run as (UID 1000). When run as root, fix
# it; otherwise say so, because a wrong owner shows up later as "unable to open database file".
if [ "$(id -u)" = 0 ]; then
  chown -R 1000:1000 "$client_dir"
elif [ "$(stat -c %u "$client_dir/data" 2>/dev/null || echo "?")" != 1000 ]; then
  echo "note: $client_dir/data is not owned by UID 1000, the user the containers run as." >&2
  echo "      On the VPS run this as the deploy user (UID 1000) or with sudo." >&2
fi

sed -e "s/@CLIENT@/$client/g" -e "s#@SITE_ADDRESS@#$site_address#g" "$DEPLOY_DIR/Caddyfile.template" > "$site_file"

# One bad site file would stop Caddy from loading ANY site, taking every client down. So check the
# whole configuration first, and on a failure remove this client's file and leave nothing behind.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx lead-bot-caddy; then
  if docker exec lead-bot-caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    docker exec lead-bot-caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
      && echo "caddy: reloaded with $site_address" || echo "caddy: validated but the reload failed; run it by hand" >&2
  else
    rm -rf "$site_file" "$client_dir"
    echo "caddy rejected the configuration for '$site_address'. Nothing was created." >&2
    exit 1
  fi
else
  echo "caddy: not running here, so it was not reloaded (the site file is in place)"
fi

cat <<EOF
created client '$client' under $client_dir
next:
  1. fill in every CHANGE_ME in $client_dir/.env   (never paste it into a chat)
  2. edit $client_dir/client.toml   (the roofer's facts and inspection hours)
  3. DNS: $host (or *.<your bot domain>) must point at this server
  4. bash $DEPLOY_DIR/update.sh $client <image tag>   (build one with deploy/build-image.sh)
worker mode: ${worker_flags:-dry run (prints texts, sends nothing)}
EOF
