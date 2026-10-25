"""Understanding help questions: the search on /help and the chat helper.

No outside service: it runs here, costs nothing and answers at once, and every
answer is a section of a help article, quoted as written -- it can't make up a
feature. How a question is read:

1. Words: lower-cased, punctuation dropped, filler words ("how do I", "the")
   dropped, and endings trimmed ("tailored", "tailoring" -> "tailor").
2. Meaning: words people use for the same thing become one ("CV" and "résumé"
   -> resume; "notification" -> alert; "quota", "credits" -> limit).
3. Typos: a word the articles never use is matched to the closest one they do
   ("resme" -> resume), if it's close enough.
4. Ranking: BM25 over every section, counting the article's title and
   keywords and the section's heading more than its body, plus a bonus for
   two-word phrases ("job board") found together.

The chat answers with the best section when the match is good enough;
otherwise it says it doesn't know and points to Contact us. A short follow-up
("and how much does it cost?") is read together with the article the
conversation is on.
"""
from __future__ import annotations

import difflib
import html
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from app.modules.help.knowledge import Article, Section

STOPWORDS = frozenset("""
a about above after again all am an and any are as at be been before being below between both but by can
could did do does doing down during each few for from further get got had has have having he her here hers
him his i if in into is it its itself just let me most my myself no nor now of off on once only or other
our ours out over own please same she should so some such than that the their them then there these they this
those through to too under until up very was way we were what when where which while who whom why will with
would you your yours yourself hi hello hey thanks thank ok okay site website tell know want need
help use using used able there's it's i'm i've i'd don't doesn't can't cannot isn't won't im ive dont doesnt cant
isnt wont also still really much one lot something anything thing things like see make how whats what's go going
whether every any
""".split())

# Words that mean the same thing here -> the word the articles use.
SYNONYMS: dict[str, str] = {
    "cv": "resume", "résumé": "resume", "resumé": "resume", "curriculum": "resume", "vitae": "resume",
    "customize": "tailor", "customise": "tailor", "personalize": "tailor", "personalise": "tailor",
    "adapt": "tailor", "rewrite": "tailor", "optimize": "tailor", "optimise": "tailor",
    "notification": "alert", "notify": "alert", "watch": "alert", "watchlist": "alert", "subscription": "alert",
    "quota": "limit", "allowance": "limit", "credit": "limit", "cap": "limit", "maximum": "limit",
    "price": "plan", "pricing": "plan", "cost": "plan", "pay": "plan", "paid": "plan", "premium": "plan",
    "upgrade": "plan", "billing": "plan", "money": "plan", "fee": "plan",
    "login": "sign", "signin": "sign", "logon": "sign", "log": "sign", "register": "signup",
    "registration": "signup", "join": "signup",
    "remove": "delete", "erase": "delete", "close": "delete", "cancel": "delete", "deactivate": "delete",
    "bookmark": "save", "favourite": "save", "favorite": "save", "shortlist": "save",
    "fit": "match", "score": "match", "rating": "match", "compatibility": "match",
    "canada": "canadian", "canadas": "canadian",
    "applicant": "ats", "parse": "ats", "robot": "ats",
    "history": "version", "revert": "version", "undo": "version", "previous": "version", "older": "version",
    "telegramm": "telegram",
    "mail": "email", "inbox": "email",
    "pwd": "password", "passcode": "password",
    "position": "job", "opening": "job", "vacancy": "job", "role": "job", "posting": "job", "listing": "job",
    "career": "job",
    "company": "employer", "companies": "employer", "employers": "employer",
    "city": "location", "town": "location", "place": "location", "near": "location", "nearby": "location",
    "distance": "radius", "km": "radius", "kilometre": "radius", "kilometer": "radius",
    "privacy": "private",
    "bug": "problem", "error": "problem", "broken": "problem", "issue": "problem", "fail": "problem",
    "wrong": "problem", "stuck": "problem",
    "contact": "contact", "support": "contact", "human": "contact", "person": "contact", "team": "contact",
    "feedback": "review", "rate": "review",
    "word": "docx", "doc": "docx", "msword": "docx",
}

GREETING = re.compile(r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|bonjour|hiya)\b[\s!.,]*$", re.I)
OVERVIEW = re.compile(r"^\s*(how (does|do) (it|this|hirlumo|the app|the site) work|what (is|does) (this|it|hirlumo|the app)"
                      r"( do| for| about)?|what can (you|i|hirlumo) do( here)?|(what is )?this (app|site|website)|"
                      r"help|menu|start|getting started)\b[\s?!.]*$", re.I)
THANKS = re.compile(r"^\s*(thanks|thank you|thx|ty|great|perfect|awesome|ok(ay)?|cool|got it)\b[\s!.,]*(thanks)?[\s!.]*$",
                    re.I)

MIN_SCORE = 2.2  # below this the best section isn't a real answer
MIN_COVERAGE = 0.6  # share of the question's words the answer must contain (two words or more)
K1, B = 1.4, 0.7
WEIGHTS = {"title": 1, "keywords": 1, "heading": 3, "body": 1}


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def stem(word: str) -> str:
    """A light suffix trim, the same for questions and articles, so different
    forms of a word meet: save / saved / saving -> "sav", title / titles -> "titl"."""
    if len(word) <= 3 or word.isdigit():
        return word
    for suffix, keep in (("ational", "ate"), ("ization", "ize"), ("isation", "ize"), ("iness", "y"),
                         ("ments", ""), ("ment", ""), ("ings", ""), ("ing", ""), ("ied", "y"), ("ies", "y"),
                         ("edly", ""), ("ed", ""), ("ers", "er"), ("ly", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: len(word) - len(suffix)] + keep
            break
    else:
        if word.endswith("es") and (word[-3] in "sxz" or word[-4:-2] in ("ch", "sh")):
            word = word[:-2]  # matches -> match, boxes -> box
        elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
            word = word[:-1]
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiouls":
        word = word[:-1]  # "fitt" -> "fit"
    return word


# The synonyms again, after trimming, so "notifications" and "CVs" find theirs.
_STEMMED = {stem(k): stem(v) for k, v in SYNONYMS.items()}


@lru_cache(maxsize=20000)
def _term(word: str) -> str | None:
    if word in STOPWORDS or len(word) < 2:
        return None
    if word not in SYNONYMS and word.endswith("s") and word[:-1] in SYNONYMS:
        word = word[:-1]  # "cvs" -> "cv"
    word = stem(SYNONYMS.get(word, word))
    return _STEMMED.get(word, word)


def words(text: str) -> list[str]:
    text = _fold(text).replace("e-mail", "email").replace("'s ", " ")
    return [w.strip("'") for w in re.findall(r"[a-z0-9][a-z0-9+#']*", text)]


def terms(text: str) -> list[str]:
    return [t for t in (_term(w) for w in words(text)) if t]


@dataclass
class Hit:
    section: Section
    score: float

    @property
    def article(self) -> Article:
        return self.section.article


@dataclass
class Index:
    sections: list[Section]
    docs: list[Counter]
    lengths: list[int]
    avg_length: float
    df: Counter
    bigrams: list[set[tuple[str, str]]]
    phrases: list[list[frozenset[str]]]  # each section's heading and "?:" questions, as sets of words
    vocabulary: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, articles: tuple[Article, ...]) -> "Index":
        sections, docs, bigrams, phrases = [], [], [], []
        for article in articles:
            head = terms(article.title) * WEIGHTS["title"] + terms(" ".join(article.keywords)) * WEIGHTS["keywords"]
            for section in article.sections:
                body = terms(section.text)
                heading = terms(section.heading) + terms(" ".join(section.asked))
                tokens = head + heading * WEIGHTS["heading"] + body
                sections.append(section)
                docs.append(Counter(tokens))
                phrases.append([frozenset(p) for p in (terms(section.heading), *map(terms, section.asked)) if p])
                bigrams.append(set(zip(body, body[1:])) | set(zip(heading, heading[1:]))
                               | set(zip(terms(article.title), terms(article.title)[1:])))
        lengths = [sum(d.values()) for d in docs]
        df: Counter = Counter()
        for d in docs:
            df.update(d.keys())
        return cls(sections, docs, lengths, (sum(lengths) / len(lengths)) if lengths else 1.0, df, bigrams,
                   phrases, set(df))

    def correct(self, term: str) -> str:
        """The closest word the articles use, for a word they don't."""
        if term in self.vocabulary or len(term) < 4 or term.isdigit():
            return term
        close = difflib.get_close_matches(term, self.vocabulary, n=1, cutoff=0.82)
        return close[0] if close else term

    def query(self, text: str) -> list[str]:
        return [self.correct(t) for t in terms(text)]

    def coverage(self, text: str, hit: "Hit") -> float:
        """How much of the question `hit`'s section speaks to (0-1)."""
        wanted = set(self.query(text))
        doc = self.docs[self.sections.index(hit.section)]
        return len([t for t in wanted if t in doc]) / len(wanted) if wanted else 0.0

    def search(self, text: str, *, limit: int = 8, prefer: str | None = None) -> list[Hit]:
        query = self.query(text)
        if not query:
            return []
        n = len(self.docs)
        pairs = set(zip(query, query[1:]))
        hits = []
        for i, doc in enumerate(self.docs):
            score = 0.0
            for term in set(query):
                tf = doc.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.lengths[i] / self.avg_length))
            if score <= 0:
                continue
            score += 1.5 * len(pairs & self.bigrams[i])
            # Asked the way the heading or one of its "?:" questions puts it: the strongest sign.
            wanted = set(query)
            best = 0.0
            for phrase in self.phrases[i]:
                shared = len(wanted & phrase)
                if shared and (shared == len(phrase) or shared >= 2):
                    best = max(best, shared / len(phrase) * shared / len(wanted))
            score += 6 * best
            if not self.sections[i].heading:
                score *= 0.8  # an article's introduction: answer with it only when nothing more specific fits
            if prefer and self.sections[i].article.slug == prefer:
                score *= 1.4
            hits.append(Hit(self.sections[i], score))
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]


_indexes: dict[int, Index] = {}


def index_for(articles: tuple[Article, ...]) -> Index:
    key = id(articles)  # the loaded articles are cached per settings, so this is stable
    if key not in _indexes:
        if len(_indexes) > 8:
            _indexes.clear()
        _indexes[key] = Index.build(articles)
    return _indexes[key]


# ---- the chat helper --------------------------------------------------------------

@dataclass
class Reply:
    kind: str  # answer | clarify (a one-word question: offer what to ask) | fallback | greeting | thanks
    text: str  # what to say (HTML: a section of an article, or our own short line)
    source: Section | None = None
    related: list[Section] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.kind == "answer"


STARTERS = ["How do I search for jobs?", "How do I tailor my resume for a job?",
            "What does the free plan include?", "How do job alerts work?"]


def suggestions_for(articles: tuple[Article, ...], page: str | None) -> list[str]:
    """Questions worth offering on `page`: its article's section headings."""
    if page:
        for article in articles:
            if any(page == p or (p != "/" and page.startswith(p.rstrip("/") + "/")) for p in article.pages):
                headings = [s.heading for s in article.sections if s.heading.endswith("?")]
                if headings:
                    return headings[:4]
    return STARTERS


def reply(articles: tuple[Article, ...], question: str, *, context: str | None = None,
          page: str | None = None) -> Reply:
    question = " ".join((question or "").split())[:300]
    if not question or GREETING.match(question):
        return Reply("greeting", "Hi! Ask me anything about using Hirlumo: searching, your resume, tailoring, "
                                 "alerts, plans or your account.", suggestions=suggestions_for(articles, page))
    if THANKS.match(question):
        return Reply("thanks", "You're welcome! Anything else?", suggestions=suggestions_for(articles, page))
    index = index_for(articles)
    if OVERVIEW.match(question):
        overview = next((s for a in articles if a.slug == "getting-started" for s in a.sections
                         if s.heading.startswith("What can I do")), None)
        if overview is not None:
            return Reply("answer", overview.html, source=overview)
    short = len(terms(question)) <= 2
    hits = index.search(question, prefer=context if short else None)
    asked = set(index.query(question))
    if len(asked) == 1 and hits and index.df[next(iter(asked))] > 0.15 * len(index.sections):
        # One common word ("resume", "jobs"): too broad to pick an answer, so offer questions.
        offers = []
        for hit in hits:
            if hit.section.heading and hit.section.heading not in offers:
                offers.append(hit.section.heading)
        return Reply("clarify", f"There's a lot about {html.escape(question.strip(' ?.!'))}. Which of these "
                                "did you mean?", suggestions=offers[:5])
    words_asked = len(set(index.query(question)))
    needed = MIN_COVERAGE if words_asked <= 4 else MIN_COVERAGE - 0.1  # long questions carry more of their own detail
    weak = (not hits or hits[0].score < MIN_SCORE
            or (words_asked >= 2 and index.coverage(question, hits[0]) < needed))
    if weak:
        return Reply("fallback",
                     "I couldn't find that in the help articles. Try asking another way, browse the "
                     '<a href="/help">Help centre</a>, or <a href="/contact">send us a message</a> and we\'ll '
                     "answer by email.",
                     suggestions=[h.section.heading for h in hits[:3] if h.section.heading] or STARTERS)
    best = hits[0].section
    related, seen = [], {best.anchor + best.article.slug}
    for hit in hits[1:]:
        key = hit.section.anchor + hit.article.slug
        if key not in seen and hit.section.heading and hit.score >= MIN_SCORE * 0.8:
            related.append(hit.section)
            seen.add(key)
        if len(related) == 3:
            break
    return Reply("answer", best.html, source=best, related=related)
