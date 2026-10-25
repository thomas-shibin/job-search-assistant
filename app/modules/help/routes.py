"""The Help centre (/help, /help/{slug}) and the chat helper's endpoint
(POST /help/ask). Open to everyone: answers come from the articles, here,
with no outside service, so they cost nothing."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.database.session import get_session
from app.modules.auth.deps import current_user
from app.modules.auth.models import User
from app.modules.auth.ratelimit import RateLimiter
from app.modules.auth.sessions import client_ip
from app.modules.help import knowledge, nlp
from app.modules.help.models import HelpQuestion
from app.modules.usage.quota import platform_limits
from app.web.templating import safe_next, templates

logger = logging.getLogger(__name__)
router = APIRouter()

QUESTION_DAYS = 180  # how long asked questions are kept
help_limiter = RateLimiter(limit=60, window_seconds=600)  # chat questions per visitor (IP)


def _articles(session: Session) -> tuple[knowledge.Article, ...]:
    return knowledge.articles(knowledge.values(platform_limits(session)))


def _log(session: Session, question: str, channel: str, *, hit: knowledge.Section | None, page: str | None,
         signed_in: bool) -> None:
    """Keep the question (never who asked), and drop ones older than QUESTION_DAYS."""
    try:
        session.add(HelpQuestion(question=question[:300], channel=channel, answered=hit is not None,
                                 article=hit.article.slug if hit else None,
                                 section=(hit.heading or hit.article.title)[:200] if hit else None,
                                 page=(page or "")[:200] or None, signed_in=signed_in))
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=QUESTION_DAYS)
        session.query(HelpQuestion).filter(HelpQuestion.created_at < cutoff).delete(synchronize_session=False)
        session.commit()
    except Exception:
        session.rollback()
        logger.warning("Couldn't log a help question", exc_info=True)


def snippet(text: str, query: str, size: int = 220) -> str:
    """A piece of `text` around the first word of `query` it contains."""
    lowered = text.lower()
    start = 0
    for word in nlp.words(query):
        if len(word) > 2 and (at := lowered.find(word[:5])) >= 0:
            start = max(0, at - 60)
            break
    piece = text[start:start + size]
    if start > 0:
        piece = "…" + piece[piece.find(" ") + 1:]
    if start + size < len(text):
        piece = piece[: piece.rfind(" ")] + "…"
    return piece


def _signed_in(user: User | None) -> bool:
    return user is not None and not user.is_guest


@router.get("/help")
def help_centre(request: Request, q: str = "", user: User | None = Depends(current_user),
                session: Session = Depends(get_session)):
    articles = _articles(session)
    q = " ".join(q.split())[:200]
    results = []
    if q:
        index = nlp.index_for(articles)
        seen = set()
        for hit in index.search(q, limit=12):
            if hit.score < nlp.MIN_SCORE * 0.6:
                continue
            key = (hit.article.slug, hit.section.anchor)
            if key in seen:
                continue
            seen.add(key)
            results.append({"section": hit.section, "snippet": snippet(hit.section.text, q)})
        _log(session, q, "search", hit=results[0]["section"] if results else None, page="/help",
             signed_in=_signed_in(user))
    return templates.TemplateResponse(request, "help/index.html", {
        "articles": articles, "q": q, "results": results, "starters": nlp.STARTERS,
    })


@router.get("/help/{slug}")
def help_article(request: Request, slug: str, user: User | None = Depends(current_user),
                 session: Session = Depends(get_session)):
    articles = _articles(session)
    article = next((a for a in articles if a.slug == slug), None)
    if article is None:
        raise HTTPException(status_code=404, detail="No such help article")
    position = articles.index(article)
    return templates.TemplateResponse(request, "help/article.html", {
        "article": article, "articles": articles,
        "previous": articles[position - 1] if position > 0 else None,
        "next": articles[position + 1] if position + 1 < len(articles) else None,
    })


def _section_json(section: knowledge.Section) -> dict:
    return {"article": section.article.title, "slug": section.article.slug, "heading": section.heading,
            "url": section.url}


@router.post("/help/ask")
def ask(request: Request, question: str = Form(""), context: str = Form(""), page: str = Form(""),
        user: User | None = Depends(current_user), session: Session = Depends(get_session)):
    """The chat helper: one question in, one answer out (JSON)."""
    if not help_limiter.allow(client_ip(request)):
        return JSONResponse({"kind": "limited", "html": "You've asked a lot of questions in a short time. Please "
                                                        "wait a few minutes, or browse the "
                                                        '<a href="/help">Help centre</a>.',
                             "suggestions": []}, status_code=429)
    question = " ".join(question.split())[:300]
    page = safe_next(page, "") or None
    context = context if re.fullmatch(r"[a-z0-9-]{1,80}", context or "") else None
    articles = _articles(session)
    answer = nlp.reply(articles, question, context=context, page=page)
    if answer.kind in ("answer", "fallback"):
        _log(session, question, "chat", hit=answer.source, page=page, signed_in=_signed_in(user))
    return JSONResponse({
        "kind": answer.kind, "html": answer.text,
        "source": _section_json(answer.source) if answer.source else None,
        "related": [_section_json(s) for s in answer.related],
        "suggestions": answer.suggestions,
    })


@router.get("/help/ask/suggestions")
def suggestions(page: str = "", session: Session = Depends(get_session)):
    """Questions to offer when the chat opens on `page`."""
    return JSONResponse({"suggestions": nlp.suggestions_for(_articles(session), safe_next(page, "") or None)})
