#!/usr/bin/env bash
# Moves Hirlumo's data (database, resumes, applications, backups) and config/
# out of this project folder to /srv/hirlumo, owned by a system user that
# can't log in, so copying a new version of the code here can never replace
# them, and nobody but root (and the app) can read them.
#
# Run once on the server, from this folder:  sudo ./scripts/move-data.sh
# Optional: sudo ./scripts/move-data.sh /some/other/place
#
# It stops the app, copies everything (the database while nothing writes to
# it), checks the copy, points .env.prod at it and starts the app again. The
# old folders are renamed to data.moved-<date> and config.moved-<date>, never
# deleted: remove them yourself once the site is confirmed working.
set -euo pipefail

TARGET="${1:-/srv/hirlumo}"
APP_USER=hirlumo
APP_ID=1990  # fixed, so the APP_UID/APP_GID in .env.prod are the same on any server
ENV_FILE=.env.prod
STAMP="$(date +%Y%m%d-%H%M%S)"

die() { echo "ERROR: $*" >&2; exit 1; }
ok() { echo "  ok: $*"; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo ./scripts/move-data.sh"
[ -f docker-compose.yml ] && [ -f "$ENV_FILE" ] || die "Run it from the project folder (with docker-compose.yml and $ENV_FILE)."
[ -f data/job_assistant.db ] || die "No data/job_assistant.db here -- has it been moved already? (see HIRLUMO_DATA_DIR in $ENV_FILE)"
[ ! -e "$TARGET/data/job_assistant.db" ] || die "$TARGET/data already has a database; refusing to overwrite it."
command -v sqlite3 >/dev/null || die "sqlite3 is needed to check the copy: sudo apt install -y sqlite3"

compose() { docker compose --env-file "$ENV_FILE" "$@"; }

echo "1. The app's own user"
if ! id "$APP_USER" >/dev/null 2>&1; then
  getent passwd "$APP_ID" >/dev/null && die "User id $APP_ID is taken; edit APP_ID in this script and APP_UID/APP_GID in $ENV_FILE."
  getent group "$APP_ID" >/dev/null || groupadd --system --gid "$APP_ID" "$APP_USER"
  useradd --uid "$APP_ID" --gid "$APP_ID" --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$APP_USER"
  ok "created system user $APP_USER (no login, no home)"
else
  ok "$APP_USER exists"
fi
[ "$(id -u "$APP_USER")" = "$APP_ID" ] || die "User $APP_USER exists with another id; set APP_ID in this script to $(id -u "$APP_USER")."

echo "2. Stopping the app (so the database isn't written while it's copied)"
compose stop app
trap 'echo "Stopped before the switch; the app still uses data/. Start it again: sudo docker compose --env-file $ENV_FILE up -d" >&2' ERR
ok "stopped"

echo "3. Copying to $TARGET"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$TARGET"
cp -a data "$TARGET/data"
cp -a config "$TARGET/config"
UNDO="the app still uses data/ -- start it again with: sudo docker compose --env-file $ENV_FILE up -d"
[ "$(sqlite3 "$TARGET/data/job_assistant.db" 'PRAGMA integrity_check')" = ok ] || die "The copied database failed its check; $UNDO"
files() { find "$1" -type f -not -name 'job_assistant.db-*' | wc -l; }  # not the -wal/-shm the check may add
[ "$(files data)" = "$(files "$TARGET/data")" ] || die "The copy has a different number of files than data/; $UNDO"
chown -R "$APP_USER:$APP_USER" "$TARGET"
chmod -R u+rwX,go-rwx "$TARGET"
ok "copied and checked"

trap - ERR

echo "4. Pointing $ENV_FILE at it"
cp -a "$ENV_FILE" "$ENV_FILE.before-move-$STAMP"
set_env() {
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"; else printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; fi
}
set_env HIRLUMO_DATA_DIR "$TARGET/data"
set_env HIRLUMO_CONFIG_DIR "$TARGET/config"
set_env APP_UID "$APP_ID"
set_env APP_GID "$APP_ID"
ok "HIRLUMO_DATA_DIR, HIRLUMO_CONFIG_DIR, APP_UID/APP_GID=$APP_ID (old file: $ENV_FILE.before-move-$STAMP)"

echo "5. Keeping the old folders out of the way"
mv data "data.moved-$STAMP"
mv config "config.moved-$STAMP"
ok "renamed to data.moved-$STAMP and config.moved-$STAMP (not deleted)"

echo "6. Starting the app"
compose up -d app
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q app)" 2>/dev/null)" = healthy ] && break
  sleep 5
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q app)" 2>/dev/null)" = healthy ] ||
  die "The app didn't become healthy -- see: sudo docker compose --env-file $ENV_FILE logs app
To go back: sudo docker compose --env-file $ENV_FILE stop app; mv data.moved-$STAMP data; mv config.moved-$STAMP config; cp $ENV_FILE.before-move-$STAMP $ENV_FILE; sudo docker compose --env-file $ENV_FILE up -d"
ok "running from $TARGET"

echo
echo "Done. Check the site, sign in, open a resume. Once it all works, remove the old copies:"
echo "  sudo rm -rf data.moved-$STAMP config.moved-$STAMP"
