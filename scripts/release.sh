#!/usr/bin/env bash
# Releases a new version as a compiled image (no source code) through the
# private registry on the server, then brings it up there. The server needs no
# code: only docker-compose.yml, its .env.prod and the data.
#
#   ./scripts/release.sh                  # test, build, check, push, bring up (next 0.N)
#   ./scripts/release.sh --version 0.9    # a version of your choosing
#   ./scripts/release.sh --push-only      # build and push; bring up later with --up
#   ./scripts/release.sh --up 0.9         # bring up a version already in the registry
#   ./scripts/release.sh --rollback 0.8   # the same: go back to an earlier version
#   ./scripts/release.sh --list           # versions in the registry, and which is running
#   ./scripts/release.sh --setup-registry # once: start the registry on the server
#   ./scripts/release.sh --no-tests       # skip the tests first
#
# Run from the project folder on your computer. DEPLOY_TARGET (user@host:folder)
# is the server and its Hirlumo folder; REGISTRY_PORT the registry's port there
# (deploy/registry/docker-compose.yml). The registry only listens on the server's
# 127.0.0.1, so pushes go through an SSH tunnel this script opens and closes.
set -euo pipefail

DEPLOY_TARGET="${DEPLOY_TARGET:-john@192.168.1.90:data/services/job-search-assistant}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"           # on the server
LOCAL_PORT="${LOCAL_REGISTRY_PORT:-$REGISTRY_PORT}"  # this end of the tunnel
REPO=hirlumo
# Sent alongside the image: what the server needs to run it. Never data/, config/ or .env files.
FILES=(docker-compose.yml deploy/Caddyfile deploy/registry/docker-compose.yml scripts/move-data.sh)

HOST="${DEPLOY_TARGET%%:*}"
FOLDER="${DEPLOY_TARGET#*:}"
SERVER_IMAGE="127.0.0.1:$REGISTRY_PORT/$REPO"
LOCAL_IMAGE="127.0.0.1:$LOCAL_PORT/$REPO"

MODE=release; VERSION=""; TESTS=true
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="${2:?--version needs a value, e.g. 0.9}"; shift ;;
    --push-only) MODE=push ;;
    --up|--rollback) MODE=up; VERSION="${2:?$1 needs a version, e.g. 0.8}"; shift ;;
    --list) MODE=list ;;
    --setup-registry) MODE=setup ;;
    --no-tests) TESTS=false ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 1 ;;
  esac
  shift
done

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
[ -f docker-compose.yml ] && [ -f Dockerfile ] || die "Run it from the project folder: ./scripts/release.sh"
[ -z "$VERSION" ] || [[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){1,2}$ ]] || die "A version looks like 0.9 or 1.2.3, not '$VERSION'."

TUNNEL_SOCKET=""
open_tunnel() {
  [ -n "$TUNNEL_SOCKET" ] && return 0
  TUNNEL_SOCKET="$(mktemp -u "${TMPDIR:-/tmp}/hirlumo-tunnel.XXXXXX")"
  ssh -f -N -M -S "$TUNNEL_SOCKET" -o ExitOnForwardFailure=yes \
    -L "127.0.0.1:$LOCAL_PORT:127.0.0.1:$REGISTRY_PORT" "$HOST" ||
    die "Couldn't open the tunnel to the registry (is port $LOCAL_PORT free here? set LOCAL_REGISTRY_PORT)."
  for _ in $(seq 1 20); do
    curl -fs -o /dev/null "http://127.0.0.1:$LOCAL_PORT/v2/" && return 0
    sleep 0.5
  done
  die "The registry on $HOST isn't answering. Start it once: ./scripts/release.sh --setup-registry"
}
close_tunnel() {
  if [ -n "$TUNNEL_SOCKET" ]; then
    ssh -S "$TUNNEL_SOCKET" -O exit "$HOST" 2>/dev/null || true
    TUNNEL_SOCKET=""
  fi
}
trap close_tunnel EXIT

registry_tags() {  # one per line, oldest version first
  curl -fs "http://127.0.0.1:$LOCAL_PORT/v2/$REPO/tags/list" 2>/dev/null |
    python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin).get('tags') or []))" 2>/dev/null |
    sort -V || true
}

check_server() {
  step "Checking the server ($HOST)"
  ssh "$HOST" "test -f '$FOLDER/.env.prod'" ||
    die "$DEPLOY_TARGET has no .env.prod -- wrong server or folder? Set DEPLOY_TARGET=user@host:folder."
}

send_files() {
  step "Sending the compose files"
  rsync -a --relative --itemize-changes "${FILES[@]}" "$DEPLOY_TARGET/"
}

bring_up() {  # $1 = version, already in the registry
  local version="$1"
  step "Bringing up $version on $HOST (sudo may ask for your password)"
  # On the server: pull; back up the database (the new version may migrate it);
  # point APP_IMAGE in .env.prod at the version, so any later restart runs the
  # same one; start it and wait until it's healthy. On failure it says how to go back.
  ssh -t "$HOST" "set -e; cd '$FOLDER'
    sudo docker pull -q '$SERVER_IMAGE:$version'
    previous=\$(sed -n 's/^APP_IMAGE=//p' .env.prod | tail -1)
    data=\$(sed -n 's/^HIRLUMO_DATA_DIR=//p' .env.prod | tail -1); data=\${data:-./data}
    if [ -f \"\$data/job_assistant.db\" ]; then
      if command -v sqlite3 >/dev/null; then
        # The folder is the app's (its nightly backups go there too); the copy is readable by root only.
        sudo test -d \"\$data/backups\" || { sudo mkdir \"\$data/backups\" && sudo chown --reference=\"\$data\" \"\$data/backups\" && sudo chmod 700 \"\$data/backups\"; }
        copy=\"\$data/backups/job_assistant-pre-$version-\$(date +%Y%m%d%H%M).db\"
        (umask 077; sudo sqlite3 \"\$data/job_assistant.db\" \".backup \$copy\")
        sudo chmod 600 \"\$copy\"
        echo \"Database backed up to \$copy\"
      else
        echo 'No sqlite3 on the server (sudo apt install -y sqlite3): starting without a fresh backup copy.'
      fi
    fi
    if grep -q '^APP_IMAGE=' .env.prod; then
      sed -i 's|^APP_IMAGE=.*|APP_IMAGE=$SERVER_IMAGE:$version|' .env.prod
    else
      echo 'APP_IMAGE=$SERVER_IMAGE:$version' >> .env.prod
    fi
    sudo docker compose --env-file .env.prod up -d --no-build app
    echo 'Waiting for the app to be healthy...'
    for _ in \$(seq 1 40); do
      s=\$(sudo docker inspect -f '{{.State.Health.Status}}' \$(sudo docker compose --env-file .env.prod ps -q app) 2>/dev/null || true)
      [ \"\$s\" = healthy ] && echo 'Healthy.' && exit 0
      sleep 5
    done
    echo 'Not healthy after 3 minutes. Logs:'; sudo docker compose --env-file .env.prod logs --tail 40 app
    [ -n \"\$previous\" ] && echo \"To go back: ./scripts/release.sh --rollback \${previous##*:}\"
    exit 1" || die "Version $version didn't start cleanly -- see above."
  echo; echo "Running $version on $HOST."
}

case "$MODE" in
  setup)
    step "Starting the registry on $HOST (sudo may ask for your password)"
    ssh "$HOST" "mkdir -p '$FOLDER/deploy/registry'"
    rsync -a deploy/registry/docker-compose.yml "$HOST:$FOLDER/deploy/registry/"
    ssh -t "$HOST" "set -e; cd '$FOLDER'
      sudo mkdir -p /srv/hirlumo-registry && sudo chown 1000:1000 /srv/hirlumo-registry && sudo chmod 700 /srv/hirlumo-registry
      sudo REGISTRY_PORT='$REGISTRY_PORT' docker compose -f deploy/registry/docker-compose.yml up -d
      sleep 3; curl -fs -o /dev/null http://127.0.0.1:$REGISTRY_PORT/v2/ && echo 'Registry is up on 127.0.0.1:$REGISTRY_PORT.'"
    exit 0 ;;
  list)
    open_tunnel
    step "Versions in the registry"
    registry_tags
    step "Running on $HOST"
    ssh "$HOST" "sed -n 's/^APP_IMAGE=//p' '$FOLDER/.env.prod'"
    exit 0 ;;
  up)
    check_server
    open_tunnel
    grep -qx "$VERSION" <<<"$(registry_tags)" || die "$VERSION isn't in the registry. See: ./scripts/release.sh --list"
    close_tunnel
    send_files
    bring_up "$VERSION"
    exit 0 ;;
esac

# ---- release / push: build a new version ------------------------------------------
check_server
open_tunnel
if [ -z "$VERSION" ]; then
  last=$(registry_tags | grep -E '^0\.[0-9]+$' | tail -1 || true)
  [ -n "$last" ] || die "No versions in the registry yet: give the first one, e.g. --version 0.9"
  VERSION="0.$(( ${last#0.} + 1 ))"
fi
! grep -qx "$VERSION" <<<"$(registry_tags)" || die "$VERSION is already in the registry: pick another --version."
echo "Version: $VERSION"

if $TESTS; then
  step "Tests"
  [ -x .venv/bin/python ] || die "No .venv here to run the tests; use --no-tests to skip them."
  .venv/bin/python -m pytest -q -x -p no:warnings || die "Tests failed; nothing was built."
fi

step "Building $REPO:$VERSION (compiled, no source)"
docker build --target release --build-arg VERSION="$VERSION" -t "$REPO:$VERSION" .

step "Checking the image"
# No source, secrets, databases, resumes or docs inside; then a start like production's.
docker run --rm --entrypoint sh "$REPO:$VERSION" -c '
  bad=$(find /app \( -name "*.py" -o -name ".env*" -o -name "*.db" -o -name "config.yaml" -o -name "*.key" \
         -o -name docs -o -name tests \) -print | head -5)
  [ -z "$bad" ] || { echo "Not allowed in the image: $bad"; exit 1; }
  [ -z "$(find /app/data /app/config -mindepth 1 -print -quit)" ] || { echo "data/ or config/ is not empty"; exit 1; }
  echo "No source, secrets or data inside."' || die "The image failed its checks; nothing was pushed."
smoke=$(mktemp -d); mkdir -p "$smoke/data" "$smoke/config"
name="hirlumo-release-check-$$"
docker run -d --name "$name" --read-only --tmpfs /tmp:size=512m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges:true -u "$(id -u):$(id -g)" -e APP_SECRET_KEY="$(openssl rand -hex 32)" \
  -v "$smoke/data:/app/data" -v "$smoke/config:/app/config" "$REPO:$VERSION" >/dev/null
ok=false
for _ in $(seq 1 60); do
  if docker exec "$name" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" 2>/dev/null; then
    ok=true; break
  fi
  sleep 1
done
if $ok; then
  for path in / /jobs /help /login; do
    code=$(docker exec "$name" python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000$path').status)" 2>/dev/null || echo failed)
    [ "$code" = 200 ] || { ok=false; echo "$path -> $code"; }
  done
fi
$ok || docker logs --tail 40 "$name"
docker rm -f "$name" >/dev/null; rm -rf "$smoke"
$ok || die "The new image didn't start cleanly on an empty database; nothing was pushed."
echo "Starts on an empty database and serves pages."

step "Pushing $VERSION to the registry on $HOST"
docker tag "$REPO:$VERSION" "$LOCAL_IMAGE:$VERSION"
docker push "$LOCAL_IMAGE:$VERSION"
docker rmi "$LOCAL_IMAGE:$VERSION" >/dev/null  # the local "$REPO:$VERSION" stays
close_tunnel

if [ "$MODE" = push ]; then
  echo; echo "Pushed $VERSION. Bring it up with: ./scripts/release.sh --up $VERSION"
  exit 0
fi
send_files
bring_up "$VERSION"
