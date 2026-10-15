"""Compatibility import for the renamed Sub2API provider."""

from __future__ import annotations

from llm_usage.providers.sub2api import Sub2ApiProvider

LlmGatewayProvider = Sub2ApiProvider
