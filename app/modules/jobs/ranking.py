"""Ordering jobs by how well they match the user: the AI fit score when a job
has one, else its keyword score; unscored jobs keep their original order.
Pure functions, shared by search results and the tracked-jobs list."""
from __future__ import annotations


def match_score(scores: dict | None) -> int | None:
    """The score a job is ranked by -- the one its badge shows: the AI fit
    score if it has been analyzed with AI, else the keyword score."""
    if not scores:
        return None
    if scores.get("fit") is not None:
        return scores["fit"].fit_score
    return scores.get("score")


def best_match_first(items: list, scores) -> list:
    """Highest matching score first; unscored jobs after them, in their
    original (newest-first) order -- the sort is stable.

    Args:
        items: Anything to sort (search cards, tracked-job rows).
        scores: Function returning an item's ``{"score": ..., "fit": ...}`` dict (or None).
    """
    def key(item):
        score = match_score(scores(item))
        return (score is None, -(score or 0))
    return sorted(items, key=key)
