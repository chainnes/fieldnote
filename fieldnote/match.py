from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


def normalize(text: str) -> str:
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def score_title(query: str, title: str) -> float:
    left, right = normalize(query), normalize(title)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def quote_in_source(quote: str, source: str) -> bool:
    """True when the quote is copied from the source, ignoring extra whitespace."""
    needle = " ".join((quote or "").split())
    hay = " ".join((source or "").split())
    return bool(needle) and needle in hay


@dataclass
class Scored:
    task_id: str
    title: str
    score: float
    owner_id: str = ""
    owner_name: str = ""
    planned_due: str = ""
    status: str = ""
    priority: str = ""


@dataclass
class LinkDecision:
    task: Scored | None = None
    cousins: list[Scored] = field(default_factory=list)
    nearby: list[Scored] = field(default_factory=list)
    strength: str = "none"  # exact | fuzzy | ambiguous | none


def decide_link(scored: list[Scored]) -> LinkDecision:
    """Link a phrase to a task only when one candidate is clearly the same work."""
    ranked = sorted(scored, key=lambda item: item.score, reverse=True)
    exact = [item for item in ranked if item.score >= 0.999]
    if len(exact) == 1:
        return LinkDecision(task=exact[0], strength="exact")
    if len(exact) > 1:
        return LinkDecision(cousins=exact[:6], strength="ambiguous")
    if ranked and ranked[0].score >= 0.84:
        top = ranked[0]
        peers = [item for item in ranked if top.score - item.score <= 0.08 and item.score >= 0.84]
        if len(peers) == 1:
            nearby = [item for item in ranked[1:4] if item.score >= 0.5]
            return LinkDecision(task=top, nearby=nearby, strength="fuzzy")
        return LinkDecision(cousins=peers[:6], strength="ambiguous")
    nearby = [item for item in ranked if item.score >= 0.66][:3]
    return LinkDecision(nearby=nearby, strength="none")
