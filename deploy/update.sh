#!/usr/bin/env bash
# lead-bot/deploy/update.sh
# Starts a client, or moves it to another image, and rolls back by itself if the new one isn't healthy.
#
#   bash deploy/update.sh <client> <image tag>     e.g. update.sh acme 3f9c2a1b7e
#   bash deploy/update.sh <client> --rollback      go back to the tag it ran before
#   bash deploy/update.sh <client> --restart       same image, new containers: picks up an edited .env
#
# Tags come from deploy/build-image.sh (lead-bot:<short sha>). Each client pins its own, which is
# how one client runs the new build first and the rest follow once it looks right.
#
# What it does, in order, and what stops it:
#   1. refuses unless the image exists and the client's .env has no CHANGE_ME left   (nothing touched)
#   2. recreates the client's two containers on the new tag (data/ is untouched; the worker's
#      restart recovery settles any text that was mid-send)
#   3. waits up to HEALTH_TIMEOUT seconds (default 90) for /health to answer 200, which needs the
#      server up AND the worker beating
#   4a. healthy: records the tag (and the old one as PREVIOUS_IMAGE_TAG in deploy.env)
#   4b. not healthy: puts the previous tag back and exits 1; a first start that fails is stopped
#
# Settings (environment): LEAD_BOT_ROOT (default /srv/lead-bot), HEALTH_TIMEOUT (default 90).
set -u

# C:/... form on Windows: docker is a native program and can't use /c/... paths.
DEPLOY_DIR="$(cd "$(dirname "$0")" && { pwd -W 2>/dev/null || pwd; })"
ROOT="${LEAD_BOT_ROOT:-/srv/lead-bot}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"
usage() { echo "usage: update.sh <client> <image tag | --rollback | --restart>" >&2; exit 2; }
die() { echo "update.sh: $1" >&2; exit 1; }

[ $# -eq 2 ] || usage
client="$1"; requested="$2"
client_dir="$ROOT/clients/$client"
deploy_env="$client_dir/deploy.env"
[ -f "$deploy_env" ] || die "no $deploy_env. Create the client first: deploy/new-client.sh $client <host>"

# deploy.env is read line by line, never sourced: it is a file on the server, but a stray line in
# it must not be able to run as a command.
read_setting() { grep -E "^$1=" "$deploy_env" | head -n 1 | cut -d= -f2-; }
current_tag="$(read_setting IMAGE_TAG)"
previous_tag="$(read_setting PREVIOUS_IMAGE_TAG)"

force_recreate=""
if [ "$requested" = "--rollback" ]; then
  [ -n "$previous_tag" ] || die "'$client' has no previous tag to go back to"
  new_tag="$previous_tag"
elif [ "$requested" = "--restart" ]; then
  # Containers read .env only when they are created, so a changed .env (a new key, a new
  # PUBLIC_BASE_URL) needs them recreated even though the image is the same.
  [ -n "$current_tag" ] || die "'$client' has never been started; start it with an image tag instead"
  new_tag="$current_tag"; force_recreate="--force-recreate"
else
  new_tag="$requested"
fi
printf '%s' "$new_tag" | grep -Eq '^[A-Za-z0-9_.-]{1,64}$' || die "bad image tag: '$new_tag'"

docker image inspect "lead-bot:$new_tag" >/dev/null 2>&1 \
  || die "no image lead-bot:$new_tag on this machine. Build it first: deploy/build-image.sh <git ref>"
if grep -q CHANGE_ME "$client_dir/.env"; then
  die "$client_dir/.env still has CHANGE_ME lines; fill them in before starting this client"
fi

compose() { # compose <image tag> <docker compose arguments...>
  local tag="$1"; shift
  # A variable in the process environment beats the same name in --env-file, which is how the tag
  # being tried replaces the one recorded in deploy.env.
  IMAGE_TAG="$tag" docker compose --env-file "$deploy_env" -p "lead-bot-$client" -f "$DEPLOY_DIR/compose.template.yaml" "$@"
}
server_container() {
  docker ps -q --filter "label=com.docker.compose.project=lead-bot-$client" --filter "label=com.docker.compose.service=server" | head -n 1
}
wait_until_healthy() { # wait_until_healthy <image tag>: true once /health is 200 on a server running that tag
  local tag="$1" waited=0 container
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    container="$(server_container)"
    if [ -n "$container" ] \
      && [ "$(docker inspect --format '{{.Config.Image}}' "$container" 2>/dev/null)" = "lead-bot:$tag" ] \
      && docker exec "$container" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2; waited=$((waited + 2))
  done
  return 1
}
write_tags() { # write_tags <IMAGE_TAG> <PREVIOUS_IMAGE_TAG>: rewrite deploy.env in one rename
  local temporary="$deploy_env.new"
  sed -e "s#^IMAGE_TAG=.*#IMAGE_TAG=$1#" -e "s#^PREVIOUS_IMAGE_TAG=.*#PREVIOUS_IMAGE_TAG=$2#" "$deploy_env" > "$temporary"
  mv -f "$temporary" "$deploy_env"
}

started_at=$SECONDS
if [ -z "$force_recreate" ] && [ "$new_tag" = "$current_tag" ] && wait_until_healthy "$new_tag"; then
  echo "'$client' is already healthy on lead-bot:$new_tag; nothing to do"; exit 0
fi

echo "== $client: ${current_tag:-(not running)} -> $new_tag${force_recreate:+ (restart)}"
compose "$new_tag" up -d --remove-orphans $force_recreate >/dev/null 2>&1 || die "docker compose could not start lead-bot:$new_tag (no change recorded)"

if wait_until_healthy "$new_tag"; then
  if [ "$new_tag" != "$current_tag" ]; then write_tags "$new_tag" "$current_tag"; fi
  echo "ok      $client is healthy on lead-bot:$new_tag ($((SECONDS - started_at)) s)"
  [ -n "$current_tag" ] && [ "$new_tag" != "$current_tag" ] && echo "        to undo: deploy/update.sh $client --rollback   (back to lead-bot:$current_tag)"
  exit 0
fi

echo "FAILED  lead-bot:$new_tag did not become healthy within ${HEALTH_TIMEOUT} s. Its last log lines:" >&2
docker logs --tail 15 "$(server_container)" >&2 2>&1 || true
if [ -n "$current_tag" ] && [ "$current_tag" != "$new_tag" ]; then
  echo "rolling back to lead-bot:$current_tag" >&2
  compose "$current_tag" up -d --remove-orphans >/dev/null 2>&1
  if wait_until_healthy "$current_tag"; then
    echo "ok      $client is healthy again on lead-bot:$current_tag ($((SECONDS - started_at)) s in total)" >&2
  else
    echo "FAILED  $client is NOT healthy on lead-bot:$current_tag either: look at 'docker logs' and the .env" >&2
  fi
elif [ -n "$force_recreate" ]; then
  # The image did not change, so there is nothing to roll back to: the most likely cause is the
  # .env that was just edited. Leave the containers as they are for inspection.
  echo "the restart did not come up healthy; check the .env you just changed ('docker logs lead-bot-$client-server-1')" >&2
else
  # A first start that fails: stop the containers so they don't restart-loop. Data is untouched.
  compose "$new_tag" stop >/dev/null 2>&1
  echo "stopped $client (it never ran successfully, so there is nothing to roll back to)" >&2
fi
exit 1
