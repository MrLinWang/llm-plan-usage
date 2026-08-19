"""Provider abstraction: the protocol every platform fetcher implements."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from llm_usage.models import PlatformResult


@runtime_checkable
class Provider(Protocol):
    """A platform usage fetcher.

    Implementations are either live (HTTP) or manual (read from config).
    """

    name: str
    display_name: str
    is_manual: bool

    def fetch(self, config: dict[str, Any]) -> PlatformResult:
        """Fetch usage for this platform given its config section.

        Errors MUST be caught and returned inside ``PlatformResult.error``
        rather than raised — a single platform failure must not break others.
        """
        ...