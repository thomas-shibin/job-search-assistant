"""AI cost-tracking summary (spec §19, Milestone 13's dashboard cost view) --
reads `AICallLog` rows so cost visibility doesn't require querying the
database by hand.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Integer, func
from sqlalchemy.orm import Session

from app.models.ai_usage import AICallLog


@dataclass
class OperationCostSummary:
    operation: str
    call_count: int
    cache_hit_count: int
    tokens_in: int
    tokens_out: int
    estimated_cost: float


def ai_cost_summary(session: Session) -> list[OperationCostSummary]:
    rows = (
        session.query(
            AICallLog.operation,
            func.count(AICallLog.id),
            func.sum(func.cast(AICallLog.cache_hit, Integer)),
            func.coalesce(func.sum(AICallLog.tokens_in), 0),
            func.coalesce(func.sum(AICallLog.tokens_out), 0),
            func.coalesce(func.sum(AICallLog.estimated_cost), 0),
        )
        .group_by(AICallLog.operation)
        .all()
    )
    return [
        OperationCostSummary(
            operation=operation,
            call_count=call_count,
            cache_hit_count=cache_hit_count or 0,
            tokens_in=int(tokens_in),
            tokens_out=int(tokens_out),
            estimated_cost=float(estimated_cost),
        )
        for operation, call_count, cache_hit_count, tokens_in, tokens_out, estimated_cost in rows
    ]
