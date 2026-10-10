#!/usr/bin/env bash
# Job Assistant installer for Linux (and macOS).
#
#   ./install.sh                 # Docker (recommended): builds and starts the app
#   ./install.sh --native        # no Docker: Python virtual environment instead
#   ./install.sh --port 8080     # use another port (default 8000)
#   ./install.sh --no-prompt     # don't ask for API keys (add them to .env later)
#
# Safe to run again: it keeps your .env, settings and data, and just rebuilds/restarts.
set -euo pipefail
cd "$(dirname "$0")"

MODE=docker
PORT=""
PROMPT=1
while [ $# -gt 0 ]; do
  case "$1" in
    --native) MODE=native ;;
    --docker) MODE=docker ;;
    --port) PORT="${2:?--port needs a number}"; shift ;;
    --no-prompt) PROMPT=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
  shift
done

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Set KEY=value in .env, replacing an existing line or appending one.
set_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    local tmp; tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k{print k"="v; next} {print}' .env > "$tmp" && mv "$tmp" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}
get_env() { grep "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }

ask_key() {  # ask_key VAR "description" -- only if not already set
  local key="$1" label="$2" value
  if [ -n "$(get_env "$key")" ]; then ok "$key already set"; return; fi
  if [ "$PROMPT" = 1 ] && [ -t 0 ]; then
    read -r -p "  $label (Enter to skip): " value
    [ -n "$value" ] && set_env "$key" "$value" && ok "$key saved"
  fi
}

bold "Job Assistant -- installing ($MODE)"

# 1. Folders and settings ------------------------------------------------------
mkdir -p data/resumes data/applications config
[ -f .env ] || { cp .env.example .env; ok "Created .env"; }
chmod 600 .env
[ -n "$PORT" ] || PORT="$(get_env APP_PORT)"; PORT="${PORT:-8000}"
set_env APP_PORT "$PORT"

bold "API keys (all optional -- you can add them to .env later)"
ask_key OPENAI_API_KEY  "OpenAI API key, for AI analysis and resume tailoring"
ask_key ADZUNA_APP_ID   "Adzuna app ID, for job search (free at developer.adzuna.com)"
ask_key ADZUNA_APP_KEY  "Adzuna app key"
ask_key TELEGRAM_BOT_TOKEN "Telegram bot token, for job alerts"

URL="http://127.0.0.1:${PORT}"

# 2a. Docker -----------------------------------------------------------------
if [ "$MODE" = docker ]; then
  bold "Checking Docker"
  command -v docker >/dev/null 2>&1 || die "Docker isn't installed.
  Ubuntu/Debian:  sudo apt install -y docker.io docker-compose-v2 && sudo usermod -aG docker \$USER   (then log out and in)
  Other Linux:    https://docs.docker.com/engine/install/
  Or run without Docker:  ./install.sh --native"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing: sudo apt install -y docker-compose-v2"
  docker info >/dev/null 2>&1 || die "Can't talk to Docker. Start it (sudo systemctl start docker) and make sure
  your user is in the docker group: sudo usermod -aG docker \$USER, then log out and back in."
  ok "Docker is ready"

  # Run the container as you, so everything it writes in data/ stays yours.
  set_env APP_UID "$(id -u)"
  set_env APP_GID "$(id -g)"

  bold "Building and starting (the first build takes a few minutes)"
  docker compose up -d --build

  printf '  Waiting for the app'
  for _ in $(seq 1 90); do
    if curl -fsS "$URL/health" >/dev/null 2>&1; then printf '\n'; ok "Running at $URL"; break; fi
    printf '.'; sleep 2
  done
  curl -fsS "$URL/health" >/dev/null 2>&1 || die "The app didn't start. See: docker compose logs app"

  START_CMD="docker compose up -d"
  STOP_CMD="docker compose down"
  LOGS_CMD="docker compose logs -f app"
  CLI="docker compose exec app python cli.py"

# 2b. Native -----------------------------------------------------------------
else
  bold "Checking Python (3.11 or newer)"
  PY=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$candidate"; break
    fi
  done
  [ -n "$PY" ] || die "Python 3.11+ not found. Ubuntu/Debian: sudo apt install -y python3 python3-venv python3-pip"
  ok "Using $($PY --version)"

  if [ ! -x .venv/bin/python ]; then
    "$PY" -m venv .venv || die "Couldn't create a virtual environment. Ubuntu/Debian: sudo apt install -y python3-venv"
  fi
  bold "Installing Python packages"
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e .
  ok "Packages installed"

  .venv/bin/alembic upgrade head >/dev/null
  ok "Database ready"

  command -v soffice >/dev/null 2>&1 && ok "LibreOffice found (PDF resumes)" ||
    warn "LibreOffice not found -- tailored resumes will be Word only. For PDFs: sudo apt install -y libreoffice-writer"
  command -v notify-send >/dev/null 2>&1 || warn "For desktop notifications: sudo apt install -y libnotify-bin"

  cat > start.sh <<EOS
#!/usr/bin/env bash
# Starts Job Assistant (dashboard + background job scans). Ctrl+C to stop.
cd "\$(dirname "\$0")"
exec .venv/bin/python cli.py serve --port ${PORT}
EOS
  chmod +x start.sh
  ok "Created ./start.sh"

  bold "Starting"
  nohup ./start.sh > data/server.log 2>&1 &
  for _ in $(seq 1 30); do curl -fsS "$URL/health" >/dev/null 2>&1 && break; sleep 1; done
  curl -fsS "$URL/health" >/dev/null 2>&1 && ok "Running at $URL (log: data/server.log)" ||
    die "The app didn't start -- see data/server.log"

  START_CMD="./start.sh"
  STOP_CMD="pkill -f 'cli.py serve --port ${PORT}'"
  LOGS_CMD="tail -f data/server.log"
  CLI=".venv/bin/python cli.py"
fi

# 3. Done ---------------------------------------------------------------------
command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1 || true
command -v open >/dev/null 2>&1 && [ "$(uname)" = Darwin ] && open "$URL" || true

bold "Done! Open $URL"
cat <<EOF
  Next, in the app:
    1. My resume  -> upload your resume (Word .docx keeps your design)
    2. Settings   -> Job search: your job titles and locations
    3. Settings   -> Address profiles: your address (optional)

  Useful commands:
    Start:   $START_CMD
    Stop:    $STOP_CMD
    Logs:    $LOGS_CMD
    Fill the search suggestions (needs Adzuna keys):  $CLI catalog-scan
EOF
