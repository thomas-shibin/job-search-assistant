from __future__ import annotations

from pathlib import Path

import pytest

from app.config.loader import ConfigError, load_config, validate_config

VALID_YAML = """
candidate:
  name: "Test Candidate"
  base_resume: "./data/resumes/base_resume.docx"

address_profiles:
  bc:
    city: "Victoria"
    province: "BC"
    country: "Canada"

search_profiles:
  - name: "BC Backend"
    enabled: true
    locations: ["Victoria, BC"]
    address_profile: "bc"
    titles: ["Senior Software Engineer"]

matching:
  minimum_score: 75
  required_or_high_value_skills: ["Java"]
  preferred_skills: ["AWS"]
  negative_keywords: ["PHP Developer"]
  weights:
    technical_skills: 30
    relevant_experience: 20
    title_similarity: 15
    seniority: 10
    platform: 10
    location: 5
    domain_experience: 5
    education: 5

resume:
  optimize_keywords: true

notifications:
  minimum_score: 80

scheduler:
  enabled: true
  interval_minutes: 15
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def _create_base_resume(tmp_path: Path) -> None:
    resume_dir = tmp_path / "data" / "resumes"
    resume_dir.mkdir(parents=True)
    (resume_dir / "base_resume.docx").write_bytes(b"fake docx")


def test_valid_config_passes(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, VALID_YAML)
    _create_base_resume(tmp_path)

    config = load_config(config_path)
    errors = validate_config(config, base_dir=tmp_path)

    assert errors == []


def test_missing_base_resume_file_fails(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, VALID_YAML)
    # deliberately do not create data/resumes/base_resume.docx

    config = load_config(config_path)
    errors = validate_config(config, base_dir=tmp_path)

    assert any("base_resume" in e for e in errors)


def test_unknown_address_profile_reference_fails(tmp_path: Path) -> None:
    bad_yaml = VALID_YAML.replace('address_profile: "bc"', 'address_profile: "does_not_exist"')
    config_path = _write_config(tmp_path, bad_yaml)
    _create_base_resume(tmp_path)

    config = load_config(config_path)
    errors = validate_config(config, base_dir=tmp_path)

    assert any("address_profile" in e for e in errors)


def test_weights_not_summing_to_100_fails(tmp_path: Path) -> None:
    bad_yaml = VALID_YAML.replace("technical_skills: 30", "technical_skills: 99")
    config_path = _write_config(tmp_path, bad_yaml)

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    assert any("100" in e for e in exc_info.value.errors)
