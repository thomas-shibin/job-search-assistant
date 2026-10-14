"""Well-known locations on disk, resolved once from this file's place in the
package (app/core/paths.py -> the project root two levels up), so moving other
modules around never changes where data is read or written."""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
