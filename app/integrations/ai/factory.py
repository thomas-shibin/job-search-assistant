from __future__ import annotations

from app.integrations.ai.base import AIProvider
from app.integrations.ai.mock import MockAIProvider
from app.integrations.ai.settings import AISettings
from app.core.config.schema import AIConfig


class AIProviderConfigError(Exception):
    """Raised when `ai.provider` in config.yaml can't actually be constructed
    (e.g. `openai` selected but OPENAI_API_KEY isn't set in `.env`)."""


def build_ai_provider(ai_config: AIConfig, settings: AISettings | None = None) -> AIProvider:
    if ai_config.provider == "mock":
        return MockAIProvider()

    settings = settings or AISettings()
    if not settings.openai_api_key:
        raise AIProviderConfigError(
            "ai.provider is 'openai' but OPENAI_API_KEY is not set. Add it to your .env file "
            "(see .env.example), or set ai.provider: mock in config.yaml to use the free "
            "heuristic provider instead."
        )
    from app.integrations.ai.openai_provider import OpenAIProvider

    return OpenAIProvider(api_key=settings.openai_api_key, model=ai_config.model)
