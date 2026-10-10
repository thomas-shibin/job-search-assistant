# Install playbook — Windows and Linux

How to put Job Assistant on another computer. Each person gets their own copy with
their own resume, settings and API keys — nothing of yours is shared.

**Quick version:** copy the app folder over, then run the installer —
`install.cmd` on Windows (double-click), `./install.sh` on Linux. It asks for the
API keys, builds and starts the app, and opens it at **http://127.0.0.1:8000**.

---

## 1. Get the app onto the laptop

Pick one:

**A. A zip from your computer (simplest).** In the project folder on *your* machine:

```bash
python scripts/package.py          # -> dist/job-assistant-YYYYMMDD.zip
```

It contains only the app — never your `.env` (keys), settings, database or resumes, and
it refuses to build if one of your keys would be included. Copy the zip over (USB,
email, cloud drive) and unzip it somewhere permanent, e.g. `C:\job-assistant` or
`~/job-assistant`.

**B. From GitHub** (once your latest changes are pushed):

```bash
git clone https://github.com/thomas-shibin/job-search-assistant.git job-assistant
```

## 2. Choose how it runs

| | **Docker** (recommended) | **Native** (no Docker) |
|---|---|---|
| Needs | Docker Desktop (Windows) / Docker Engine (Linux) | Python 3.11+, optionally LibreOffice |
| PDF resumes + fonts | included | install LibreOffice yourself |
| Starts with the computer | yes, automatically | start it from the shortcut / `start.sh` |
| Best for | most people | machines where Docker isn't allowed |

---

## 3. Windows

### 3a. Windows with Docker (recommended)

One-time prerequisite — Docker Desktop (needs Windows 10/11 64-bit; uses WSL 2):

```powershell
winget install -e --id Docker.DockerDesktop
```

Restart Windows, open **Docker Desktop** once and wait until it says it's running.

Install the app — double-click **`install.cmd`** in the app folder, or in PowerShell:

```powershell
cd C:\job-assistant
powershell -ExecutionPolicy Bypass -File install.ps1
```

Options: `-Port 8080` (if 8000 is taken), `-NoPrompt` (skip the key questions).

### 3b. Windows without Docker

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id TheDocumentFoundation.LibreOffice    # optional: PDF resumes
```

Open a **new** PowerShell window (so Python is on PATH), then:

```powershell
cd C:\job-assistant
powershell -ExecutionPolicy Bypass -File install.ps1 -Native
```

It creates a virtual environment, installs the packages, sets up the database, adds a
**Job Assistant** shortcut to the desktop and starts the app. Keep that window open
while using the app (minimise it); close it to stop.

<details><summary>Manual native steps (if the script can't run)</summary>

```powershell
cd C:\job-assistant
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
copy .env.example .env          # then open .env in Notepad and add your keys
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe cli.py serve      # http://127.0.0.1:8000
```
</details>

---

## 4. Linux (Ubuntu / Debian; other distros similar)

### 4a. Linux with Docker (recommended)

One-time prerequisite:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER      # then log out and back in
```

Install the app:

```bash
cd ~/job-assistant
./install.sh                       # options: --port 8080, --no-prompt
```

### 4b. Linux without Docker

```bash
sudo apt install -y python3 python3-venv python3-pip
sudo apt install -y libreoffice-writer libnotify-bin   # optional: PDF resumes, desktop notifications
cd ~/job-assistant
./install.sh --native
```

The app runs in the background (log: `data/server.log`); `./start.sh` starts it again later.

<details><summary>Manual native steps (if the script can't run)</summary>

```bash
cd ~/job-assistant
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env               # then add your keys: nano .env
.venv/bin/alembic upgrade head
.venv/bin/python cli.py serve      # http://127.0.0.1:8000
```
</details>

macOS works like Linux: install Docker Desktop (`brew install --cask docker`) or Python
3.11+ (`brew install python@3.12`), then `./install.sh` or `./install.sh --native`.

---

## 5. First-time setup (in the app)

Open **http://127.0.0.1:8000** (or the port you chose). Every setting already has its
default (applied by the database migrations on first start); only the personal ones are
blank, so:

1. **My resume** → upload the resume. A Word **.docx** keeps its exact design in every
   tailored resume; a PDF gets a plain layout. Check "What the app read from it".
2. **Settings → Job search** → job titles, locations, keywords and country.
3. **Settings → Address profiles** → the address to show on tailored resumes (optional;
   street and postal code optional too).
4. **Settings → AI** → leave as is, or turn on `enabled` for AI on every scanned job.
5. **Watchlist** → Telegram alerts (optional): paste the bot token into `.env`, press
   *Start* on your bot in Telegram, then **Detect chat ID** and **Send test message**.
6. Fill the search suggestions from the job market (once; ~50 Adzuna requests):
   Settings → Search catalog → **Refresh catalog**.
7. **Settings → Retention** → how long found jobs are kept (default 7 days; jobs you've
   applied to etc. are always kept).

### API keys (all optional, all in `.env`)

| Key | For | Where to get it |
|---|---|---|
| `OPENAI_API_KEY` | AI analysis + resume tailoring (~$0.001 per use) | https://platform.openai.com/api-keys |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Job search (free, ~250 requests/day) | https://developer.adzuna.com/ |
| `TELEGRAM_BOT_TOKEN` | Job alerts | Telegram → @BotFather → /newbot |

After editing `.env`, restart the app (below) so it picks the keys up.

---

## 6. Everyday use

| | Docker (in the app folder) | Native |
|---|---|---|
| Open | http://127.0.0.1:8000 | same |
| Start | `docker compose up -d` (also starts by itself with the computer) | Windows: **Job Assistant** desktop shortcut · Linux: `./start.sh` |
| Stop | `docker compose down` | Windows: close its window · Linux: `pkill -f "cli.py serve"` |
| Restart (after editing `.env`) | `docker compose up -d --force-recreate` | stop, then start |
| Logs | `docker compose logs -f app` | Windows: its window · Linux: `tail -f data/server.log` |
| Any command | `docker compose exec app python cli.py <command>` | `.venv/bin/python cli.py <command>` (Windows: `.venv\Scripts\python.exe cli.py <command>`) |

All commands: see "Command reference" in [README.md](README.md).

**Start automatically (native only).** Windows: press `Win+R`, type `shell:startup`,
copy the **Job Assistant** desktop shortcut into that folder. Linux (systemd):

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/job-assistant.service <<EOF
[Unit]
Description=Job Assistant
[Service]
WorkingDirectory=$HOME/job-assistant
ExecStart=$HOME/job-assistant/start.sh
Restart=on-failure
[Install]
WantedBy=default.target
EOF
systemctl --user enable --now job-assistant
```

---

## 7. Updating to a newer version

Your data lives in `data/`, `config/config.yaml` and `.env` — an update never touches them.

1. Stop the app.
2. Replace the app files: unzip the new package **over** the old folder (or `git pull`).
3. Run the installer again (`install.cmd` / `./install.sh`, add `-Native` / `--native` if
   that's how it was installed). It rebuilds, applies database updates and starts the app.

## 8. Backup, or move to another computer

Copy these three things (with the app stopped):

```
data/            # database, resumes, generated applications
config/          # starting settings
.env             # API keys -- keep it private
```

To restore: install the app as above, stop it, copy them back into the app folder, start it.

## 9. Uninstall

```bash
# Docker: in the app folder
docker compose down
docker image rm job-search-assistant
```

Then delete the app folder (after backing up `data/` if you want to keep anything). For a
native Windows install also delete the desktop shortcut; on Linux,
`systemctl --user disable --now job-assistant` if you set that up.

---

## 10. Troubleshooting

| Problem | Fix |
|---|---|
| `install.cmd` says Docker isn't running | Open Docker Desktop, wait for "running", run `install.cmd` again. |
| "Port is already allocated" / page doesn't load | Another program uses the port: reinstall with `-Port 8080` / `--port 8080`. |
| Linux: `permission denied ... docker.sock` | `sudo usermod -aG docker $USER`, log out and back in. |
| Windows: "running scripts is disabled" | Use `install.cmd`, or `powershell -ExecutionPolicy Bypass -File install.ps1`. |
| Windows native: `py`/`python` not found | Install Python (step 3b) and open a **new** PowerShell window. |
| Jobs page: "Set ADZUNA_APP_ID and ADZUNA_APP_KEY" | Add both to `.env`, then restart the app. |
| "Analyze with AI" fails: `OPENAI_API_KEY is not set` | Add it to `.env`, then restart. Check the key has billing enabled. |
| Tailored resumes are DOCX only, no PDF | Native installs: install LibreOffice (steps 3b / 4b). Docker includes it. |
| Tailored resume doesn't keep your design | Upload the Word (.docx) version on **My resume** — PDFs can't be edited in place. |
| Anything else | Look at the logs (section 6) — the last lines usually say what's wrong. |
