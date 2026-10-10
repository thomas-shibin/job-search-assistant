"""Build a clean zip of the app to install on another computer:

    python scripts/package.py            # -> dist/job-assistant-YYYYMMDD.zip
    python scripts/package.py --image thomasshibin90/jobfinderapp:0.1
                                         # -> dist/job-assistant-docker-YYYYMMDD.zip

The --image package has no source code: just the installers, docker-compose.yml
(without the build step) and a .env.example with APP_IMAGE set, so Docker pulls
the published image. Push that image to Docker Hub first.

The full package contains only the project's own files (what git tracks, plus new files git
isn't told to ignore) -- never .env, config/config.yaml, the database,
resumes or generated applications. As a last check it refuses to build if any
secret from your .env appears in a file it would ship.
"""
from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP = "job-assistant"  # folder name inside the zip
NEVER = re.compile(r"(^|/)(\.env|config/config\.yaml|data/.+\.(db|db-journal|docx|pdf|log))$")
# Resumes and other documents never ship -- only the made-up sample resumes the tests use.
DOCUMENT = re.compile(r"\.(pdf|docx?|odt|rtf)$", re.I)
SAMPLE_DOCUMENTS = "tests/fixtures/"


def project_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout.decode()
    return sorted(
        f for f in out.split("\0")
        if f and (ROOT / f).is_file() and not NEVER.search(f)
        and not (DOCUMENT.search(f) and not f.startswith(SAMPLE_DOCUMENTS))
    )


def secrets() -> list[str]:
    env = ROOT / ".env"
    if not env.exists():
        return []
    values = []
    for line in env.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        key = key.strip()
        is_secret = any(word in key for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")) or key == "ADZUNA_APP_ID"
        if key and not key.startswith("#") and is_secret:
            value = value.strip().strip("'\"")
            if len(value) >= 8:
                values.append(value)
    return values


# What an --image package ships (Docker only, no source).
IMAGE_FILES = ("install.sh", "install.ps1", "install.cmd", "config/config.example.yaml")
IMAGE_README = """Job Assistant (Docker)

1. Install Docker: Windows `winget install -e --id Docker.DockerDesktop` (restart, open it
   once); Ubuntu/Debian `sudo apt install -y docker.io docker-compose-v2`.
2. Run the installer in this folder: double-click install.cmd (Windows) or ./install.sh (Linux/macOS).
   It asks for the API keys (all optional), downloads {image} and opens http://127.0.0.1:8000
3. In the app: My resume -> upload your resume; Settings -> Job search.

Your data stays in data/, config/ and .env in this folder.
Update: change APP_IMAGE in .env to the new tag, then run the installer again.
Stop: docker compose down    Start: docker compose up -d    Logs: docker compose logs -f app
"""


def image_package(image: str) -> int:
    out = ROOT / "dist" / f"{TOP}-docker-{date.today():%Y%m%d}.zip"
    out.parent.mkdir(exist_ok=True)
    compose = "".join(line for line in (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines(True)
                      if line.strip() != "build: .")
    env = re.sub(r"(?m)^APP_IMAGE=.*$", f"APP_IMAGE={image}", (ROOT / ".env.example").read_text(encoding="utf-8"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in IMAGE_FILES:
            info = zipfile.ZipInfo.from_file(ROOT / name, f"{TOP}/{name}")
            info.compress_type = zipfile.ZIP_DEFLATED
            if name.endswith(".sh"):
                info.external_attr = (0o755 << 16)
            zf.writestr(info, (ROOT / name).read_bytes())
        zf.writestr(f"{TOP}/docker-compose.yml", compose)
        zf.writestr(f"{TOP}/.env.example", env)
        zf.writestr(f"{TOP}/README.txt", IMAGE_README.format(image=image))
        for keep in ("data/resumes/.gitkeep", "data/applications/.gitkeep"):
            zf.writestr(f"{TOP}/{keep}", "")
    print(f"Created {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB) -- installs {image}, no source code.")
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--image":
        return image_package(sys.argv[2])
    if len(sys.argv) > 1:
        print(__doc__)
        return 2
    files = project_files()
    leaked = []
    for secret in secrets():
        for name in files:
            try:
                if secret in (ROOT / name).read_text(encoding="utf-8", errors="ignore"):
                    leaked.append(name)
            except OSError:
                pass
    if leaked:
        print("Refusing to package: a secret from .env appears in:", *sorted(set(leaked)), sep="\n  ")
        return 1

    out = ROOT / "dist" / f"{TOP}-{date.today():%Y%m%d}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            info = zipfile.ZipInfo.from_file(ROOT / name, f"{TOP}/{name}")
            if name.endswith(".sh"):
                info.external_attr = (0o755 << 16)  # keep install.sh executable
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, (ROOT / name).read_bytes())
        for keep in ("data/resumes/.gitkeep", "data/applications/.gitkeep"):
            if keep not in files:
                zf.writestr(f"{TOP}/{keep}", "")
    print(f"Created {out.relative_to(ROOT)} ({len(files)} files, {out.stat().st_size // 1024} KB)")
    print("No .env, settings, database or resumes included.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
