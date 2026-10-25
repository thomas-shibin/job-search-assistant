"""The help articles (app/modules/help/articles/*.md): loading, the live
numbers in them, and turning their Markdown into HTML.

An article is a Markdown file with a small header:

    ---
    title: Plans and limits
    summary: One line, shown in lists and search results.
    keywords: price, cost, quota, allowance        (extra words people use)
    pages: /plans, /jobs                           (pages the article is about)
    order: 11
    ---

Each `## heading` starts a section: the unit the search and the chat helper
answer with, so headings are written the way people ask ("How do I ...?").
A line starting `?: ` under a heading lists other ways people ask that
question ("?: change my cv for a job, adjust my resume"): the search and the
chat use them, and they aren't shown. `{{ name }}` is replaced with a live
platform setting (see `values`), so the limits the help gives are always the
real ones.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ARTICLES_DIR = Path(__file__).resolve().parent / "articles"


@dataclass
class Section:
    article: "Article"
    heading: str  # "" for the text before the first heading
    anchor: str
    markdown: str
    asked: list[str] = field(default_factory=list)  # the "?: " lines: other ways of asking

    @property
    def html(self) -> str:
        return render(self.markdown)

    @property
    def text(self) -> str:
        return plain(self.markdown)

    @property
    def url(self) -> str:
        return f"/help/{self.article.slug}" + (f"#{self.anchor}" if self.anchor else "")


@dataclass
class Article:
    slug: str
    title: str
    summary: str
    keywords: list[str]
    pages: list[str]
    order: int
    markdown: str
    sections: list[Section] = field(default_factory=list)

    @property
    def html(self) -> str:
        return render(self.markdown, anchors=True)

    @property
    def url(self) -> str:
        return f"/help/{self.slug}"


def values(platform) -> dict[str, str]:
    """The settings an article may quote, by name."""
    return {
        "free_searches_total": str(platform.free_searches_total),
        "free_ai_actions_total": str(platform.free_ai_actions_total),
        "ai_actions_per_hour": str(platform.member_ai_actions_per_hour),
        "searches_per_hour": str(platform.member_searches_per_hour),
        "guest_searches_per_hour": str(platform.guest_searches_per_hour),
        "max_search_pages": str(platform.max_search_pages),
        "contact_email": "hirlumo@gmail.com",
    }


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def _header(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---"):
        return {}, raw
    head, _, body = raw[3:].partition("\n---")
    meta = {}
    for line in head.strip().splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            meta[key.strip().lower()] = value.strip()
    return meta, body.lstrip("\n")


def _fill(text: str, settings: dict[str, str]) -> str:
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: settings.get(m.group(1), m.group(0)), text)


def _split(article: Article) -> list[Section]:
    sections, heading, lines, asked = [], "", [], []

    def close():
        if heading or "".join(lines).strip():
            sections.append(Section(article, heading, slugify(heading), "\n".join(lines).strip(), list(asked)))

    for line in article.markdown.splitlines():
        if line.startswith("## "):
            close()
            heading, lines, asked = line[3:].strip(), [], []
        elif line.startswith("?:"):
            asked.extend(q.strip() for q in line[2:].split(",") if q.strip())
        else:
            lines.append(line)
    close()
    return sections


def _without_asked(markdown: str) -> str:
    return "\n".join(line for line in markdown.splitlines() if not line.startswith("?:"))


@lru_cache(maxsize=8)
def _load(settings_key: tuple[tuple[str, str], ...]) -> tuple[Article, ...]:
    settings = dict(settings_key)
    articles = []
    for path in sorted(ARTICLES_DIR.glob("*.md")):
        meta, body = _header(path.read_text(encoding="utf-8"))
        article = Article(
            slug=path.stem, title=meta.get("title", path.stem.replace("-", " ").title()),
            summary=_fill(meta.get("summary", ""), settings),
            keywords=[k.strip() for k in meta.get("keywords", "").split(",") if k.strip()],
            pages=[p.strip() for p in meta.get("pages", "").split(",") if p.strip()],
            order=int(meta.get("order", "99") or 99), markdown=_without_asked(_fill(body, settings)),
        )
        article.sections = _split(Article(**{**article.__dict__, "markdown": _fill(body, settings)}))
        for section in article.sections:
            section.article = article
        articles.append(article)
    return tuple(sorted(articles, key=lambda a: (a.order, a.title)))


def articles(settings: dict[str, str]) -> tuple[Article, ...]:
    return _load(tuple(sorted(settings.items())))


def find(settings: dict[str, str], slug: str) -> Article | None:
    return next((a for a in articles(settings) if a.slug == slug), None)


# ---- Markdown (the small part of it the articles use) -----------------------------

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text: str) -> str:
    text = html.escape(text, quote=True)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", text)

    def link(m: re.Match) -> str:
        label, target = m.group(1), html.unescape(m.group(2))
        if target.startswith(("/", "#", "mailto:")):
            return f'<a href="{html.escape(target, quote=True)}">{label}</a>'
        if target.startswith("https://"):
            return f'<a href="{html.escape(target, quote=True)}" target="_blank" rel="noopener">{label}</a>'
        return label  # anything else is shown as plain text
    return _LINK.sub(link, text)


def render(markdown: str, *, anchors: bool = False) -> str:
    """Headings (##, ###), paragraphs, bulleted and numbered lists, > tips,
    **bold**, *italic*, `code` and [links](/path). Everything is escaped."""
    out: list[str] = []
    para: list[str] = []
    list_kind: str | None = None
    tip: list[str] = []

    def flush_para():
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def flush_list():
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    def flush_tip():
        if tip:
            out.append(f'<div class="help-tip">{render(chr(10).join(tip))}</div>')
            tip.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith(">"):
            flush_para()
            flush_list()
            tip.append(line[1:].lstrip())
            continue
        flush_tip()
        if not line.strip():
            flush_para()
            flush_list()
            continue
        heading = re.match(r"^(#{2,4})\s+(.*)$", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        number = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if heading:
            flush_para()
            flush_list()
            level = len(heading.group(1)) + (0 if anchors else 1)
            text = heading.group(2).strip()
            anchor = f' id="{slugify(text)}"' if anchors and len(heading.group(1)) == 2 else ""
            out.append(f"<h{level}{anchor}>{_inline(text)}</h{level}>")
        elif bullet or number:
            flush_para()
            kind = "ul" if bullet else "ol"
            if list_kind != kind:
                flush_list()
                out.append(f"<{kind}>")
                list_kind = kind
            out.append(f"<li>{_inline((bullet or number).group(1))}</li>")
        elif list_kind and raw.startswith("  "):
            out[-1] = out[-1][:-5] + " " + _inline(line.strip()) + "</li>"  # a list item's next line
        else:
            flush_list()
            para.append(line.strip())
    flush_para()
    flush_list()
    flush_tip()
    return "\n".join(out)


def plain(markdown: str) -> str:
    """The words of an article, for searching and snippets."""
    text = _LINK.sub(r"\1", markdown)
    text = re.sub(r"[`*>#]", "", text)
    text = re.sub(r"^\s*[-]\s+|^\s*\d+[.)]\s+", "", text, flags=re.M)
    return " ".join(text.split())
