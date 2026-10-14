"""Settings: the schema (schema.py), loading config.yaml (loader.py), storing
each user's and the platform's sections in the database (store.py), and the
settings in effect for a request (current.py)."""
from app.core.config.loader import ConfigError, load_config, validate_config
from app.core.config.schema import (
    AddressProfile,
    AIConfig,
    AppConfig,
    CandidateConfig,
    MatchingConfig,
    MatchingWeights,
    NotificationsConfig,
    ResumeConfig,
    SchedulerConfig,
    SearchProfile,
)

__all__ = [
    "AppConfig",
    "CandidateConfig",
    "AddressProfile",
    "SearchProfile",
    "MatchingConfig",
    "MatchingWeights",
    "AIConfig",
    "ResumeConfig",
    "NotificationsConfig",
    "SchedulerConfig",
    "load_config",
    "validate_config",
    "ConfigError",
]
