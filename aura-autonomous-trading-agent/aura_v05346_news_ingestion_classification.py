#!/usr/bin/env python3
"""
AURA v0.5.3.46 — News Ingestion + Classification (Alpaca-only, deterministic).

INGESTION AND CLASSIFICATION ONLY. Never scores sentiment, never determines
trade direction or size, never blocks or authorizes anything. This module's
entire job is to fetch news items from one authoritative source, tag each
with deterministic, evidence-preserving categories, and persist them to a
minimal append-only ledger so `.47` (Market sentiment scoring) has a stable,
auditable input instead of every consumer re-fetching and re-classifying
the same articles independently.

Why this exists (v0.5.5 Final Implementation Baseline, milestone `.46`)
------------------------------------------------------------------------
An audit (2026-09-13) confirmed zero news/sentiment implementation anywhere
in the canonical `aura_v053xx` chain -- two modules (`.34`, `.38`) even
carry explicit non-goal disclaimers reserving this territory for a future
milestone. `requirements.txt` has never listed a news or NLP dependency.
`.46` is the first milestone in the "Market State / Research" track (the
roadmap's other alternative-intelligence tracks -- sentiment scoring `.47`,
Elliott Wave `.48`, the AI proposal pipeline `.49` -- all build on it).

Reuse scan (Martin's explicit A/B/C/D framework, performed BEFORE writing
any new code)
------------------------------------------------------------------------
  - DELTAX v2 `market_news_ingestion.py` / `company_news_ingestion.py`: (C)
    architecturally useful shape (fetch -> normalize -> persist), but
    incompatible -- built against Finnhub/Marketaux with an `openai`-based
    classifier (`news_ai_processor.py`) and a Postgres store, none of which
    fit AURA's atomic-file convention or its Alpaca-first scoping decision.
  - DELTAX v2 `direction_router.py`: explicitly NOT adapted. This project's
    own prior audit (`AURA_DELTAX_MEXC_deltax_v2_typed_boundary_audit`)
    already flagged its core flaw -- AI-derived sentiment fed straight into
    a LONG/SHORT decision with no typed validation boundary on the AI's
    output. `.46` avoids that class of bug entirely by not calling an AI at
    all (see "Scoping decisions" below); the boundary problem itself
    remains explicitly `.49`'s job (adversarial-challenge sub-role +
    non-expansive-repair sanitizer, per the Final Baseline's amended scope).
  - Hermes Trader (competitor, reference-only) `pipeline/sentiment/score.py`:
    (D) not applicable to `.46` -- an LLM-prompt-driven scorer, and scoring
    is `.47`'s job, not `.46`'s.
  - `mexc_bot/`: grepped fully for `news|sentiment` -- zero matches. No
    prior art here at all.
  - CAURA, BABIL, optionwright: no news/sentiment code found in any of the
    three (confirmed by direct read/grep, not assumed).
  - TradePilot / ORION / Dark Wolf Sentinel: only presentation PDFs exist
    on disk for these three (no unpacked source tree) -- not auditable as
    code; not claimed as reused.
  - `alpaca-py` (already an installed, already-a-project-dependency
    library): exposes `alpaca.data.historical.news.NewsClient.get_news()`
    and `alpaca.data.requests.NewsRequest` -- verified by direct
    `inspect.getsource()` read, not assumed from documentation. This is a
    real, already-available, previously-unused capability and is the one
    concrete asset `.46` builds on.
  Conclusion: nothing existing is directly reusable (A) or adaptable (B)
  for `.46`'s actual scope; the DELTAX/Hermes prior art is (C) at best and
  is referenced for shape/anti-pattern lessons only, never copied.

Scoping decisions (Martin, AskUserQuestion, 2026-09-13 -- reproduced here
so a future reader does not have to reconstruct them from chat history)
------------------------------------------------------------------------
  1. Source scope: Alpaca only. `alpaca-py`'s `NewsClient` is already an
     installed, unused dependency -- zero new API keys or dependencies.
     Finnhub/Marketaux (named in the original DELTAX-derived requirement
     wording) are explicitly deferred to a later, additive milestone once
     Alpaca-only ingestion is proven; nothing here forecloses adding them.
  2. Classification method: deterministic keyword/rule-based only. No LLM
     call anywhere in this module. Sentiment (a judgment about whether a
     classified item is bullish/bearish) is explicitly `.47`'s job, not
     `.46`'s -- `.46` only tags WHAT KIND of news an item is (earnings,
     macro, M&A, regulatory, ...), never what it MEANS for a position.
  3. Earnings blackout / event-risk windows: explicitly OUT of `.46`'s
     scope, despite one reconciliation doc's note to "fold into .45/gate
     framework [now .46]" -- the currently governing Final Implementation
     Baseline's `.46` table entry says only "News ingestion + classification"
     with no blackout-window language, and blackout-window enforcement is a
     risk-gate concern (hard-blocks a trade), architecturally distinct from
     ingesting/classifying news content. Deferred to its own future
     milestone with its own audit, not built as a rushed side-effect here.
  4. Persistence: a new, minimal, append-only event ledger, mirroring
     `.43`'s `equity_history.json` pattern exactly (whole-file atomic
     read-modify-write, single `state_hash` over the full event list) --
     not `.40`'s heavier per-record hash-chain/event-sourced state machine,
     which exists to adjudicate a single order's lifecycle and does not fit
     an open-ended, ever-growing collection of independent news items.

Governance constraints this module is built to respect (from
`AURA_DELTAX_MEXC_canonical_architecture_and_governance_2026-09-08.md`
§4.3/§4.4, stated there for all "alternative-intelligence" domains,
news included)
------------------------------------------------------------------------
  "Every Level 1 observation... carries an explicit age/decay[.]" --
      `created_at`/`updated_at`/`fetched_at` are preserved verbatim on every
      persisted item specifically so a consumer (`.47`) can compute this;
      `.46` does not itself judge an item stale or fresh.
  "A single unverified news item... should not be able to move Market
  State on its own[.]" -- `.46` produces no aggregate signal, score, or
      decision of any kind; it persists individually-tagged items only.
      Any "is this corroborated by enough sources" judgment is explicitly
      out of scope here and left to `.47`.
  "No alternative-intelligence signal may determine trade direction or
  size on its own[.]" -- this module has no path to an order, a position,
      or a sizing decision anywhere in its call graph. It is not imported
      by, and does not import, any execution-authority module.

Disclosed limitations (like `.43`, stated plainly rather than hidden)
------------------------------------------------------------------------
  - Same sandboxed network egress limitation disclosed in `.42`/`.43`:
    `alpaca.markets` could not be reached from this session, so
    `fetch_alpaca_news` below is verified against the actual installed
    `alpaca-py` library source and exercised against injected fakes
    (matching this repo's established test convention), not against a
    live account. A live smoke test is the right next check before `.47`
    depends on this data in production.
  - Classification is keyword-based and English-only. A headline using
    unusual phrasing may land in `UNCATEGORIZED` rather than a specific
    category -- this is a disclosed false-negative risk, not a bug: an
    under-classified item is safer than a fabricated one, and every
    classification decision carries its own matched-keyword evidence so a
    human can audit exactly why an item was (or was not) tagged a given
    way.
  - Deduplication keys on `(source, external_id)` -- Alpaca's own article
    id. If Alpaca ever republishes an updated article under a new id, it
    is treated as a new item rather than an edit to the old one; this
    module does not attempt article-versioning.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ------------------------------------------------------------------------
# stable_json / sha256_text / now_iso / _atomic_write_json -- copied, not
# imported, matching this repo's own established convention (every
# aura_v053NN module is independently file-loadable and defines these
# itself; see e.g. `.43`'s own copy, which this is byte-for-byte identical
# to).
# ------------------------------------------------------------------------

def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Same tmp-then-os.replace() pattern as `.29`/`.43`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def _iso(value: Any) -> str | None:
    """Normalizes a datetime (or an already-ISO string) to an ISO-8601
    string. Never fabricates a timestamp: None in, None out."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class NewsIngestionError(Exception):
    pass


# ------------------------------------------------------------------------
# Deterministic classification. Every category is a plain keyword/regex
# rule; every match is retained as evidence. Non-exclusive: one item can
# (and often does) match multiple categories. No confidence score, no
# ranking, no "primary category" -- inventing a false sense of precision
# here is exactly the failure mode Martin flagged for `.44`'s limits and
# is avoided the same way here: report what matched, nothing more.
# ------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "EARNINGS": (
        r"\bearnings\b", r"\beps\b", r"quarterly results", r"revenue guidance",
        r"\bbeats?\s+estimates?\b", r"\bmisses?\s+estimates?\b", r"\bq[1-4]\s+results\b",
        r"\btop[- ]line\b", r"\bbottom[- ]line\b",
    ),
    "GUIDANCE": (
        r"\bguidance\b", r"\bforecast(s|ed)?\b", r"\boutlook\b", r"\braises?\s+outlook\b",
        r"\bcuts?\s+outlook\b",
    ),
    "ANALYST_ACTION": (
        r"\bupgrades?\b", r"\bdowngrades?\b", r"\bprice target\b", r"initiat(e[sd]?|ion of)\s+coverage",
        r"\breiterates?\s+rating\b", r"\banalyst\b",
    ),
    "MERGER_ACQUISITION": (
        r"\bmerger\b", r"\bmergers\b", r"\bacquisition\b", r"\bacquires?\b", r"\bacquired\b",
        r"\btakeover\b", r"\bm&a\b", r"\bbuyout\b",
    ),
    "REGULATORY": (
        r"\bsec\b", r"\bftc\b", r"\bregulator(y|s)?\b", r"\bantitrust\b", r"\bfda\b",
        r"\bprobe\b", r"\binvestigation\b", r"\bsanctions?\b",
    ),
    "LEGAL": (
        r"\blawsuit\b", r"\blitigation\b", r"\bsettlement\b", r"\bcourt\b", r"\bsues?\b", r"\bsued\b",
    ),
    "DIVIDEND": (
        r"\bdividend\b", r"\bbuyback\b", r"\bshare repurchase\b", r"\bex-dividend\b",
    ),
    "MANAGEMENT_CHANGE": (
        r"\bceo\b", r"\bcfo\b", r"\bcoo\b", r"\bresigns?\b", r"\bresignation\b",
        r"\bappoints?\b", r"\bappointment\b", r"\bsteps? down\b", r"\bnames?\s+new\b",
    ),
    "PRODUCT": (
        r"\blaunch(es|ed)?\b", r"\bunveils?\b", r"\brecall(s|ed)?\b", r"\bnew product\b",
    ),
    "MACRO": (
        r"\bfederal reserve\b", r"\bfed\b", r"\binterest rates?\b", r"\binflation\b",
        r"\bgdp\b", r"\bjobs report\b", r"\bcpi\b", r"\bunemployment\b", r"\btariffs?\b",
    ),
}

UNCATEGORIZED = "UNCATEGORIZED"
CLASSIFICATION_METHOD = "DETERMINISTIC_KEYWORD_RULES"


def classify_news_item(headline: str, summary: str) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Pure function: text in, (categories, evidence) out. Never raises on
    empty/odd input -- worst case is an UNCATEGORIZED result with empty
    evidence, never a fabricated category."""
    text = f"{headline or ''}\n{summary or ''}"
    evidence: dict[str, tuple[str, ...]] = {}
    for category, patterns in CATEGORY_KEYWORDS.items():
        matched = tuple(p for p in patterns if re.search(p, text, re.IGNORECASE))
        if matched:
            evidence[category] = matched
    categories = tuple(sorted(evidence.keys())) if evidence else (UNCATEGORIZED,)
    return categories, evidence


# ------------------------------------------------------------------------
# Canonical, source-neutral schema (source-neutral in shape even though
# only ALPACA is populated today, so `.46` does not have to be redesigned
# if/when Finnhub/Marketaux are added later).
# ------------------------------------------------------------------------

@dataclass(frozen=True)
class NewsItem:
    source: str                          # "ALPACA" (ingestion venue; only value produced today)
    external_id: str                     # venue's own article id, as a string
    origin_source: str | None            # the ARTICLE's actual originating outlet, e.g. "Benzinga",
                                          # "GlobeNewswire" -- verbatim from Alpaca's own `News.source`
                                          # field. None only if the upstream field was genuinely absent
                                          # (never fabricated). Distinct from `source` above: `source` is
                                          # always "ALPACA" (which venue we fetched from); `origin_source`
                                          # is which wire actually wrote the piece -- the field `.47`'s
                                          # source-diversity/corroboration check depends on (added
                                          # 2026-09-13, see `.47`'s completion report for why: `.47`
                                          # cannot tell genuine multi-outlet corroboration from merely
                                          # many articles off the same wire without this field).
    headline: str
    summary: str
    url: str | None
    author: str
    created_at: str | None               # ISO-8601, verbatim from the source
    updated_at: str | None               # ISO-8601, verbatim from the source
    symbols: tuple[str, ...]             # source-tagged related symbols (authoritative, not inferred)
    categories: tuple[str, ...]
    classification_evidence: dict[str, tuple[str, ...]]
    classification_method: str
    content_hash: str                    # sha256(headline+summary) -- integrity/dedup aid
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "external_id": self.external_id,
            "origin_source": self.origin_source,
            "headline": self.headline,
            "summary": self.summary,
            "url": self.url,
            "author": self.author,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "symbols": list(self.symbols),
            "categories": list(self.categories),
            "classification_evidence": {k: list(v) for k, v in self.classification_evidence.items()},
            "classification_method": self.classification_method,
            "content_hash": self.content_hash,
            "fetched_at": self.fetched_at,
        }


@dataclass(frozen=True)
class NewsFetchStatus:
    source: str                          # "ALPACA"
    status: str                          # "SUCCESS" | "FAILED" | "NOT_CONFIGURED"
    error: str | None
    fetched_at: str
    items_fetched: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "status": self.status, "error": self.error,
            "fetched_at": self.fetched_at, "items_fetched": self.items_fetched,
        }


@dataclass(frozen=True)
class IngestionResult:
    fetch_status: NewsFetchStatus
    items_fetched: int
    items_new: int
    items_duplicate: int
    ledger_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetch_status": self.fetch_status.to_dict(),
            "items_fetched": self.items_fetched,
            "items_new": self.items_new,
            "items_duplicate": self.items_duplicate,
            "ledger_path": self.ledger_path,
        }


# ------------------------------------------------------------------------
# Fetch. Verified against alpaca-py 0.44.0's actual installed source
# (`NewsClient.get_news`, `NewsRequest`, `News`, `NewsSet` -- all read via
# `inspect.getsource()`, not assumed): `NewsSet.data["news"]` is the
# `list[News]`; `News.id` is `int`; `News.symbols` is always present
# (possibly empty) per that model's own docstring. `client` is duck-typed
# (any object exposing `.get_news(request)` returning something with a
# `.data` mapping containing a `"news"` list) so tests can inject a fake
# without depending on alpaca-py's pydantic construction.
# ------------------------------------------------------------------------

def fetch_alpaca_news(
    client: Any,
    *,
    symbols: tuple[str, ...] | None = None,
    start: Any = None,
    end: Any = None,
    limit: int = 50,
    fetched_at: str | None = None,
) -> tuple[list[NewsItem], NewsFetchStatus]:
    fetched_at = fetched_at or now_iso()

    if client is None:
        return [], NewsFetchStatus(
            source="ALPACA", status="NOT_CONFIGURED", error=None,
            fetched_at=fetched_at, items_fetched=0,
        )

    try:
        request_params = _build_news_request(symbols=symbols, start=start, end=end, limit=limit)
        news_set = client.get_news(request_params)
        raw_articles = list(getattr(news_set, "data", {}).get("news", []))
    except Exception as exc:  # noqa: BLE001 -- any fetch failure is FAILED, never a fabricated empty success
        return [], NewsFetchStatus(
            source="ALPACA", status="FAILED", error=f"{type(exc).__name__}:{exc}",
            fetched_at=fetched_at, items_fetched=0,
        )

    items: list[NewsItem] = []
    for article in raw_articles:
        headline = getattr(article, "headline", "") or ""
        summary = getattr(article, "summary", "") or ""
        categories, evidence = classify_news_item(headline, summary)
        items.append(NewsItem(
            source="ALPACA",
            external_id=str(getattr(article, "id")),
            origin_source=(getattr(article, "source", None) or None),
            headline=headline,
            summary=summary,
            url=getattr(article, "url", None),
            author=getattr(article, "author", "") or "",
            created_at=_iso(getattr(article, "created_at", None)),
            updated_at=_iso(getattr(article, "updated_at", None)),
            symbols=tuple(getattr(article, "symbols", None) or ()),
            categories=categories,
            classification_evidence=evidence,
            classification_method=CLASSIFICATION_METHOD,
            content_hash=sha256_text(stable_json({"headline": headline, "summary": summary})),
            fetched_at=fetched_at,
        ))

    return items, NewsFetchStatus(
        source="ALPACA", status="SUCCESS", error=None,
        fetched_at=fetched_at, items_fetched=len(items),
    )


def _build_news_request(*, symbols: tuple[str, ...] | None, start: Any, end: Any, limit: int) -> Any:
    """Builds a real `alpaca.data.requests.NewsRequest` when the SDK is
    importable, falling back to a plain object with the same attributes
    otherwise (this module must remain importable/testable even where
    alpaca-py is not installed, matching this repo's convention of never
    hard-failing an entire module import on an optional live dependency)."""
    kwargs = {
        "symbols": ",".join(symbols) if symbols else None,
        "start": start,
        "end": end,
        "limit": limit,
    }
    try:
        from alpaca.data.requests import NewsRequest  # type: ignore
        return NewsRequest(**kwargs)
    except Exception:
        class _PlainNewsRequest:
            def __init__(self, **kw: Any) -> None:
                for k, v in kw.items():
                    setattr(self, k, v)
        return _PlainNewsRequest(**kwargs)


# ------------------------------------------------------------------------
# Ledger -- persisted, atomic whole-file read-modify-write. Deliberately
# mirrors `.43`'s `equity_history.json` pattern exactly (a single
# `state_hash` over the full event list, not a per-record hash chain like
# `.40`'s), per Martin's explicit scoping answer: this is an open-ended,
# ever-growing collection of independent items, not a single order's
# adjudicated lifecycle.
# ------------------------------------------------------------------------

def _news_ledger_path(state_dir: Path) -> Path:
    return state_dir / "news_events.json"


def load_news_ledger(state_dir: Path) -> list[dict[str, Any]]:
    path = _news_ledger_path(state_dir)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    events = data.get("events", []) if isinstance(data, dict) else []
    return events if isinstance(events, list) else []


def append_news_ledger(state_dir: Path, items: list[NewsItem]) -> tuple[int, int]:
    """Whole-file atomic rewrite. Dedups on (source, external_id) -- the
    source's own stable article id, not content_hash, since a source may
    legitimately update summary/url text on the same id and that is still
    the same article, not a new one. Returns (new_count, duplicate_count).
    """
    events = load_news_ledger(state_dir)
    existing_keys = {(e.get("source"), e.get("external_id")) for e in events}
    new_count = 0
    duplicate_count = 0
    for item in items:
        key = (item.source, item.external_id)
        if key in existing_keys:
            duplicate_count += 1
            continue
        events.append(item.to_dict())
        existing_keys.add(key)
        new_count += 1
    events.sort(key=lambda e: (e.get("created_at") or "", e.get("external_id") or ""))
    body: dict[str, Any] = {"events": events}
    body["state_hash"] = sha256_text(stable_json({"events": events}))
    _atomic_write_json(_news_ledger_path(state_dir), body)
    return new_count, duplicate_count


# ------------------------------------------------------------------------
# Orchestration entry point.
# ------------------------------------------------------------------------

def ingest_and_classify_news(
    client: Any,
    state_dir: Path,
    *,
    symbols: tuple[str, ...] | None = None,
    start: Any = None,
    end: Any = None,
    limit: int = 50,
    fetched_at: str | None = None,
) -> IngestionResult:
    """Fetches from Alpaca, classifies deterministically, and persists new
    items to the ledger. On any fetch failure or unconfigured client, the
    ledger is left untouched -- a failed fetch never overwrites a
    previously-good ledger with an empty one."""
    items, status = fetch_alpaca_news(
        client, symbols=symbols, start=start, end=end, limit=limit, fetched_at=fetched_at,
    )
    ledger_path = str(_news_ledger_path(state_dir))
    if status.status != "SUCCESS":
        return IngestionResult(
            fetch_status=status, items_fetched=0, items_new=0, items_duplicate=0,
            ledger_path=ledger_path,
        )
    new_count, duplicate_count = append_news_ledger(state_dir, items)
    return IngestionResult(
        fetch_status=status,
        items_fetched=len(items),
        items_new=new_count,
        items_duplicate=duplicate_count,
        ledger_path=ledger_path,
    )
