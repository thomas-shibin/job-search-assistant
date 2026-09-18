"""Per-call token/cost bookkeeping shared by every `AIProvider` implementation
(spec §19 cost tracking) -- kept out of the `AIProvider` protocol itself so
the protocol stays a pure "what does this call return" contract; callers that
need cost data read `provider.last_usage` after the call instead.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AIUsage:
    tokens_in: int | None = None
    tokens_out: int | None = None
    estimated_cost: float | None = None
