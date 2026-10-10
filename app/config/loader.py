from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.config.schema import AppConfig


class ConfigError(Exception):
    """Raised when config.yaml fails schema validation."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def load_config_dict(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: Path) -> AppConfig:
    """Load and schema-validate config.yaml. Raises ConfigError with human-readable messages."""
    raw = load_config_dict(path)
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(errors) from exc


def validate_config(config: AppConfig, base_dir: Path) -> list[str]:
    """Cross-field checks that schema validation alone can't express: file existence and
    references between config sections. `base_dir` is the project root that relative
    paths in config.yaml (e.g. candidate.base_resume) are resolved against.
    """
    errors: list[str] = []

    base_resume_path = Path(config.candidate.base_resume)
    if not base_resume_path.is_absolute():
        base_resume_path = (base_dir / base_resume_path).resolve()
    if not config.candidate.base_resume.strip():
        errors.append("No resume yet: upload yours on the My resume page.")
    elif not base_resume_path.is_file():
        errors.append(
            f"candidate.base_resume: file not found at '{base_resume_path}' "
            f"(configured as '{config.candidate.base_resume}')"
        )

    known_address_profiles = set(config.address_profiles.keys())
    for sp in config.search_profiles:
        if sp.address_profile not in known_address_profiles:
            errors.append(
                f"search_profiles[{sp.name}].address_profile: "
                f"'{sp.address_profile}' is not defined under address_profiles"
            )

    return errors
