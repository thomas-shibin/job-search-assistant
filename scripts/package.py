"""Build a clean zip of the app to install on another computer:

    python scripts/package.py            # -> dist/job-assistant-YYYYMMDD.zip

Contains only the project's own files (what git tracks, plus new files git
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


def project_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout.decode()
    return sorted(f for f in out.split("\0") if f and (ROOT / f).is_file() and not NEVER.search(f))


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


def main() -> int:
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
