#!/usr/bin/env bash
# Copies a new version of the code to the server and restarts the site there.
#
#   ./scripts/deploy.sh               # test, copy, rebuild and restart, check it's healthy
#   ./scripts/deploy.sh --copy-only   # just copy the files
#   ./scripts/deploy.sh --no-tests    # skip the tests first
#
# Run from the project folder on your computer. The server is DEPLOY_TARGET
# (user@host:folder), default below. It sends only the code and build files
# listed in FILES -- never data/, config/ or any .env file, so the server's
# data and settings are never replaced -- and deletes nothing there.
set -euo pipefail

DEPLOY_TARGET="${DEPLOY_TARGET:-john@192.168.1.90:data/services/job-search-assistant}"
FILES=(app alembic alembic.ini cli.py main.py pyproject.toml
       Dockerfile .dockerignore docker-compose.yml scripts/move-data.sh)

HOST="${DEPLOY_TARGET%%:*}"
FOLDER="${DEPLOY_TARGET#*:}"
COPY_ONLY=false; TESTS=true
for arg in "$@"; do
  case "$arg" in
    --copy-only) COPY_ONLY=true ;;
    --no-tests) TESTS=false ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)" >&2; exit 1 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

[ -f docker-compose.yml ] && [ -d app ] || die "Run it from the project folder: ./scripts/deploy.sh"
for f in "${FILES[@]}"; do [ -e "$f" ] || die "Missing $f here."; done
command -v rsync >/dev/null || die "rsync is needed: sudo apt install -y rsync"

if $TESTS; then
  step "Tests"
  [ -x .venv/bin/python ] || die "No .venv here to run the tests; use --no-tests to skip them."
  .venv/bin/python -m pytest -q -x || die "Tests failed; nothing was copied."
fi

step "Checking the server ($HOST)"
# The right folder has the server's own .env.prod; refuse anything else.
ssh "$HOST" "test -f '$FOLDER/.env.prod'" ||
  die "$DEPLOY_TARGET has no .env.prod -- wrong server or folder? Set DEPLOY_TARGET=user@host:folder."
MOVED=$(ssh "$HOST" "grep -q '^HIRLUMO_DATA_DIR=.' '$FOLDER/.env.prod' && echo yes || echo no")

step "Copying the code"
rsync -a --relative --itemize-changes --exclude '__pycache__' --exclude '*.pyc' \
  "${FILES[@]}" "$DEPLOY_TARGET/"

if $COPY_ONLY; then
  echo; echo "Copied. To restart: ssh -t $HOST 'cd $FOLDER && sudo docker compose --env-file .env.prod up -d --build'"
  exit 0
fi

step "Rebuilding and restarting (sudo may ask for your password)"
ssh -t "$HOST" "cd '$FOLDER' && sudo docker compose --env-file .env.prod up -d --build app && \
  echo 'Waiting for the app to be healthy...' && \
  for i in \$(seq 1 40); do \
    s=\$(sudo docker inspect -f '{{.State.Health.Status}}' \$(sudo docker compose --env-file .env.prod ps -q app) 2>/dev/null); \
    [ \"\$s\" = healthy ] && echo 'Healthy.' && exit 0; sleep 5; \
  done; \
  echo 'Not healthy after 3 minutes. Logs:'; sudo docker compose --env-file .env.prod logs --tail 40 app; exit 1" ||
  die "The new version didn't start cleanly -- see the logs above."

echo
echo "Deployed to $HOST."
if [ "$MOVED" = no ]; then
  echo "Reminder: the data is still in the code folder. Move it out once (see INSTALL.md):"
  echo "  ssh -t $HOST 'cd $FOLDER && sudo ./scripts/move-data.sh'"
fi
