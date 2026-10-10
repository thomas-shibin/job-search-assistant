# Job Assistant installer for Windows.
#
#   Double-click install.cmd, or in PowerShell:
#   powershell -ExecutionPolicy Bypass -File install.ps1             # Docker Desktop (recommended)
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Native     # no Docker: Python instead
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Port 8080  # another port (default 8000)
#   ... -NoPrompt                                                     # don't ask for API keys
#
# Safe to run again: it keeps your .env, settings and data, and just rebuilds/restarts.
param(
    [switch]$Native,
    [int]$Port = 0,
    [switch]$NoPrompt
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Say($text)  { Write-Host "`n$text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "  [ok] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "`nError: $text" -ForegroundColor Red; exit 1 }

function Get-EnvValue($key) {
    if (-not (Test-Path .env)) { return "" }
    $line = Get-Content .env | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
    if ($line) { return $line.Substring($key.Length + 1) } else { return "" }
}
function Set-EnvValue($key, $value) {
    $lines = @(); if (Test-Path .env) { $lines = @(Get-Content .env) }
    $found = $false
    $lines = $lines | ForEach-Object { if ($_ -match "^$key=") { $found = $true; "$key=$value" } else { $_ } }
    if (-not $found) { $lines += "$key=$value" }
    # UTF-8 without BOM, so Docker Compose and Python both read it cleanly.
    [IO.File]::WriteAllLines((Join-Path $PSScriptRoot ".env"), [string[]]$lines, (New-Object Text.UTF8Encoding $false))
}
function Ask-Key($key, $label) {
    if (Get-EnvValue $key) { Ok "$key already set"; return }
    if ($NoPrompt) { return }
    $value = Read-Host "  $label (Enter to skip)"
    if ($value) { Set-EnvValue $key $value.Trim(); Ok "$key saved" }
}
function Wait-Healthy($url, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    Write-Host -NoNewline "  Waiting for the app"
    while ((Get-Date) -lt $deadline) {
        try { Invoke-WebRequest "$url/health" -UseBasicParsing -TimeoutSec 3 | Out-Null; Write-Host ""; return $true }
        catch { Write-Host -NoNewline "."; Start-Sleep -Seconds 2 }
    }
    Write-Host ""; return $false
}

$mode = if ($Native) { "native" } else { "Docker" }
Say "Job Assistant -- installing ($mode)"

# 1. Folders and settings ------------------------------------------------------
New-Item -ItemType Directory -Force -Path data\resumes, data\applications, config | Out-Null
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Ok "Created .env" }
if ($Port -eq 0) { $saved = Get-EnvValue "APP_PORT"; $Port = if ($saved) { [int]$saved } else { 8000 } }
Set-EnvValue "APP_PORT" $Port

Say "API keys (all optional -- you can add them to .env later)"
Ask-Key "OPENAI_API_KEY" "OpenAI API key, for AI analysis and resume tailoring"
Ask-Key "ADZUNA_APP_ID" "Adzuna app ID, for job search (free at developer.adzuna.com)"
Ask-Key "ADZUNA_APP_KEY" "Adzuna app key"
Ask-Key "TELEGRAM_BOT_TOKEN" "Telegram bot token, for job alerts"

$url = "http://127.0.0.1:$Port"

if (-not $Native) {
    # 2a. Docker Desktop -------------------------------------------------------
    Say "Checking Docker Desktop"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail "Docker Desktop isn't installed.
  Install it:  winget install -e --id Docker.DockerDesktop   (then restart Windows and open Docker Desktop once)
  Or run without Docker:  install.cmd -Native"
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop isn't running. Start it from the Start menu, wait until it says 'running', then run this again." }
    Ok "Docker is ready"
    Set-EnvValue "APP_UID" 1000
    Set-EnvValue "APP_GID" 1000

    Say "Building and starting (the first build takes a few minutes)"
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { Fail "docker compose failed -- see the messages above." }
    if (-not (Wait-Healthy $url 180)) { Fail "The app didn't start. See: docker compose logs app" }
    Ok "Running at $url"
    $startCmd = "docker compose up -d"; $stopCmd = "docker compose down"; $logsCmd = "docker compose logs -f app"
    $cli = "docker compose exec app python cli.py"
}
else {
    # 2b. Native ---------------------------------------------------------------
    Say "Checking Python (3.11 or newer)"
    $py = $null
    foreach ($candidate in @("py -3.13", "py -3.12", "py -3.11", "python")) {
        $exe, $arg = $candidate.Split(" ", 2)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $check = if ($arg) { & $exe $arg -c "import sys; print(sys.version_info >= (3, 11))" 2>$null } else { & $exe -c "import sys; print(sys.version_info >= (3, 11))" 2>$null }
        if ($check -eq "True") { $py = $candidate; break }
    }
    if (-not $py) { Fail "Python 3.11+ not found. Install it:  winget install -e --id Python.Python.3.12   (then open a new window and run this again)" }
    Ok "Using $py"

    if (-not (Test-Path .venv\Scripts\python.exe)) {
        $exe, $arg = $py.Split(" ", 2)
        if ($arg) { & $exe $arg -m venv .venv } else { & $exe -m venv .venv }
    }
    Say "Installing Python packages"
    & .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    & .venv\Scripts\python.exe -m pip install --quiet -e .
    if ($LASTEXITCODE -ne 0) { Fail "Installing packages failed -- see the messages above." }
    Ok "Packages installed"
    & .venv\Scripts\alembic.exe upgrade head | Out-Null
    Ok "Database ready"

    $soffice = (Get-Command soffice -ErrorAction SilentlyContinue) -or
        (Test-Path "$env:ProgramFiles\LibreOffice\program\soffice.exe") -or
        (Test-Path "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe")
    if ($soffice) { Ok "LibreOffice found (PDF resumes)" }
    else { Warn "LibreOffice not found -- tailored resumes will be Word only. For PDFs: winget install -e --id TheDocumentFoundation.LibreOffice" }

    # A start script and a desktop shortcut to it.
    Set-Content -Path start.cmd -Encoding ASCII -Value @"
@echo off
rem Starts Job Assistant (dashboard + background job scans). Close this window to stop it.
cd /d "%~dp0"
start "" $url
.venv\Scripts\python.exe cli.py serve --port $Port
"@
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Job Assistant.lnk"))
        $link.TargetPath = Join-Path $PSScriptRoot "start.cmd"
        $link.WorkingDirectory = $PSScriptRoot
        $link.Save()
        Ok "Desktop shortcut 'Job Assistant' created"
    } catch { Warn "Couldn't create a desktop shortcut; use start.cmd instead." }

    Say "Starting"
    Start-Process -FilePath (Join-Path $PSScriptRoot "start.cmd") -WorkingDirectory $PSScriptRoot -WindowStyle Minimized
    if (-not (Wait-Healthy $url 60)) { Fail "The app didn't start -- open start.cmd to see the error." }
    Ok "Running at $url"
    $startCmd = "double-click 'Job Assistant' on the desktop (or start.cmd)"
    $stopCmd = "close the Job Assistant window"; $logsCmd = "shown in the Job Assistant window"
    $cli = ".venv\Scripts\python.exe cli.py"
}

# 3. Done ---------------------------------------------------------------------
Start-Process $url
Say "Done! Open $url"
Write-Host @"
  Next, in the app:
    1. My resume  -> upload your resume (Word .docx keeps your design)
    2. Settings   -> Job search: your job titles and locations
    3. Settings   -> Address profiles: your address (optional)

  Useful commands (in this folder):
    Start:   $startCmd
    Stop:    $stopCmd
    Logs:    $logsCmd
    Fill the search suggestions (needs Adzuna keys):  $cli catalog-scan
"@
