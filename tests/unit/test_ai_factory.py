"""Tests for `build_ai_provider` -- the switch between `ai.provider: mock`
and `ai.provider: openai` in config.yaml, and the config error raised when
`openai` is selected without an API key (Milestone 5).
"""
from __future__ import annotations

import pytest

from app.core.config.schema import AIConfig
from app.integrations.ai.factory import AIProviderConfigError, build_ai_provider
from app.integrations.ai.mock import MockAIProvider
from app.integrations.ai.openai_provider import OpenAIProvider
from app.integrations.ai.settings import AISettings


def test_mock_provider_selected_without_needing_settings():
    provider = build_ai_provider(AIConfig(provider="mock"))
    assert isinstance(provider, MockAIProvider)


def test_openai_provider_selected_with_api_key_present():
    settings = AISettings(openai_api_key="sk-test-key")
    provider = build_ai_provider(AIConfig(provider="openai", model="gpt-4o-mini"), settings)
    assert isinstance(provider, OpenAIProvider)


def test_openai_provider_without_api_key_raises_config_error():
    settings = AISettings(openai_api_key=None)
    with pytest.raises(AIProviderConfigError, match="OPENAI_API_KEY"):
        build_ai_provider(AIConfig(provider="openai"), settings)


def test_unknown_provider_rejected_by_config_schema():
    with pytest.raises(ValueError, match="ai.provider"):
        AIConfig(provider="anthropic")
