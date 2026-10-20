"""Smart search: what a member typed in the job search box, read into titles,
places and filters by the rules in app/modules/jobs/query.py. The analysis
provider is asked only when the rules can't read the search.

- The vocabulary (job-title words, companies, places) is built from the
  shared search catalog and job pool, and kept in memory for an hour.
- The provider is asked only when all of these hold:
  - words are left that no job title uses and spelling can't fix;
  - `ai.search_assist` is on and a real provider is configured;
  - the member has made fewer than AI_READS_PER_HOUR such calls this hour.
  After that limit, the rules are used alone.
- Answers are kept in memory for a day, so the same words cost once. Each
  call is logged with its cost (AICallLog, operation "search_interpret"), and
  the provider never sees who is searching.
- If the provider fails, the rules' reading is used: a search never fails
  because of this.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.integrations.ai.base import SearchInterpretation
from app.integrations.ai.factory import AIProviderConfigError, build_ai_provider
from app.integrations.ai.settings import AISettings
from app.modules.auth.models import User
from app.modules.jobs import postings, query
from app.modules.jobs.places import parse_radius
from app.modules.jobs.catalog.models import LocationType, SearchTerm, TermKind
from app.modules.jobs.pool.models import PoolJob
from app.modules.usage.ai_calls import AICallLog
from app.modules.usage.quota import utcnow
from app.shared.terms import normalize_term

logger = logging.getLogger(__name__)

AI_READS_PER_HOUR = 20
OPERATION = "search_interpret"
MAX_TEXT = 300  # characters of a search sent to the provider
_VOCABULARY_SECONDS = 3600
_ANSWER_SECONDS = 24 * 3600
_ANSWERS_KEPT = 1000
_TERM_WEIGHT = 5  # a catalog term counts like several postings using the word

_lock = threading.Lock()
_vocabulary: tuple[float, query.Vocabulary] | None = None
_answers: OrderedDict[str, tuple[float, SearchInterpretation]] = OrderedDict()


@dataclass
class Interpretation:
    reading: query.Reading = field(default_factory=query.Reading)
    used_ai: bool = False
    not_a_search: bool = False  # the provider says this isn't a job search at all


def clear_cache() -> None:
    global _vocabulary
    with _lock:
        _vocabulary = None
        _answers.clear()


def vocabulary(session: Session) -> query.Vocabulary:
    global _vocabulary
    with _lock:
        if _vocabulary is not None and time.monotonic() - _vocabulary[0] < _VOCABULARY_SECONDS:
            return _vocabulary[1]
    texts = [(title, 1) for (title,) in session.query(PoolJob.title)]
    texts += [(company, 1) for (company,) in session.query(PoolJob.company).distinct()]
    terms = session.query(SearchTerm.kind, SearchTerm.label, SearchTerm.job_count, SearchTerm.location_type).all()
    texts += [(label, _TERM_WEIGHT) for kind, label, _, _ in terms if kind != TermKind.LOCATION.value]
    places = [(label, count) for kind, label, count, location_type in terms
              if kind == TermKind.LOCATION.value and location_type != LocationType.REMOTE.value]
    job_terms = [label for kind, label, _, _ in terms if kind in (TermKind.TITLE.value, TermKind.KEYWORD.value)]
    built = query.Vocabulary.build(texts, places, job_terms)
    with _lock:
        _vocabulary = (time.monotonic(), built)
    return built


def interpret(session: Session, user: User, text: str) -> Interpretation:
    """Read `text`; ask the provider only for what the rules couldn't read."""
    vocab = vocabulary(session)
    reading = query.read(text, vocab)
    if not reading.unknown:
        return Interpretation(reading)
    answer = _ask_provider(session, user, text)
    if answer is None:
        return Interpretation(reading)
    if not answer.is_job_search:
        return Interpretation(reading, used_ai=True, not_a_search=True)
    combined = _combine(answer, reading, vocab)
    if combined.is_empty:
        return Interpretation(reading, used_ai=True, not_a_search=True)
    return Interpretation(combined, used_ai=True)


def _combine(answer: SearchInterpretation, rules: query.Reading, vocab: query.Vocabulary) -> query.Reading:
    """The provider's titles and places; the rules' filters, with the provider's
    filling the gaps. Every value is checked against what the page allows."""
    reading = query.Reading(
        workplaces=list(rules.workplaces), employment_types=list(rules.employment_types), levels=list(rules.levels),
        min_salary=rules.min_salary, days=rules.days, radius_km=rules.radius_km or parse_radius(answer.radius_km),
    )
    for title in answer.titles[:3]:
        title = " ".join(str(title).split())[:80]
        if title:
            reading.add("titles", title)
    for place in [*rules.locations, *answer.locations[:3]]:
        place = " ".join(str(place).split())[:80]
        if place:
            reading.add("locations", vocab.place(place) or place)
    if not reading.workplaces and answer.workplace in postings.WORKPLACES:
        reading.add("workplaces", answer.workplace)
    if not reading.employment_types and answer.employment_type in postings.EMPLOYMENT_TYPES:
        reading.add("employment_types", answer.employment_type)
    if not reading.levels and answer.level in ("entry", "mid"):
        reading.add("levels", answer.level)
    if reading.min_salary is None and answer.min_salary and 10_000 <= answer.min_salary <= 1_000_000:
        reading.min_salary = int(answer.min_salary)
    if reading.days is None and answer.days and answer.days > 0:
        reading.days = query._snap_days(answer.days)
    return reading


def _reads_this_hour(session: Session, user: User) -> int:
    return session.query(func.count(AICallLog.id)).filter(
        AICallLog.user_id == user.id, AICallLog.operation == OPERATION,
        AICallLog.created_at >= utcnow() - timedelta(hours=1),
    ).scalar() or 0


def _ask_provider(session: Session, user: User, text: str) -> SearchInterpretation | None:
    try:
        config = user_config(session).ai
    except ConfigError:
        return None
    if not config.search_assist or config.provider == "mock":
        return None
    key = normalize_term(text)[:MAX_TEXT]
    with _lock:
        kept = _answers.get(key)
        if kept is not None and time.monotonic() - kept[0] < _ANSWER_SECONDS:
            _answers.move_to_end(key)
            return kept[1]
    if _reads_this_hour(session, user) >= AI_READS_PER_HOUR:
        return None
    try:
        provider = build_ai_provider(config, AISettings())
        answer = provider.interpret_search(text[:MAX_TEXT])
    except AIProviderConfigError:
        return None
    except Exception:
        logger.warning("Search interpretation failed; using the search box rules alone", exc_info=True)
        return None
    usage = getattr(provider, "last_usage", None)
    session.add(AICallLog(
        provider=config.provider, operation=OPERATION,
        tokens_in=usage.tokens_in if usage else None, tokens_out=usage.tokens_out if usage else None,
        estimated_cost=usage.estimated_cost if usage else None, cache_hit=False,
    ))
    session.commit()
    if answer is not None:
        with _lock:
            _answers[key] = (time.monotonic(), answer)
            while len(_answers) > _ANSWERS_KEPT:
                _answers.popitem(last=False)
    return answer
