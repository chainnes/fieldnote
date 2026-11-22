"""Turn a source into model-shaped proposals.

The mock provider uses this. A live model is asked to do the same job and is
then passed through the same schema and validator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from fieldnote.match import Scored, decide_link, normalize
from fieldnote.schemas import ReviewPacket

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "new",
        re.compile(
            r"Create new task:\s*(?P<title>.+?)\s*\(due\s*(?P<due>\d{4}-\d{2}-\d{2})\)",
            re.I,
        ),
    ),
    (
        "new",
        re.compile(
            r"add a task for\s+(?P<title>.+?)\s+and target\s+(?P<due>\d{4}-\d{2}-\d{2})",
            re.I,
        ),
    ),
    (
        "shift",
        re.compile(
            r"Shift\s+(?P<title>.+?)\s+from\s+(?P<frm>\d{4}-\d{2}-\d{2})\s+to\s+(?P<due>\d{4}-\d{2}-\d{2})",
            re.I,
        ),
    ),
    (
        "blocked",
        re.compile(r"Mark\s+(?P<title>.+?)\s+as blocked\b", re.I),
    ),
    (
        "assign",
        re.compile(
            r"(?P<owner>[A-Z][a-z]+ [A-Z][a-z]+)\s+to update\s+(?P<title>.+?)\s+by\s+(?P<due>\d{4}-\d{2}-\d{2})"
        ),
    ),
    (
        "assign",
        re.compile(
            r"please\s+(?P<owner>[A-Z][a-z]+ [A-Z][a-z]+)\s+update the\s+(?P<title>.+?)\s+by\s+(?P<due>\d{4}-\d{2}-\d{2})",
            re.I,
        ),
    ),
    (
        "owner",
        re.compile(
            r"Confirm owner for\s+(?P<title>.+?)\s*\(tentative:\s*(?P<owner>[^)]+)\)",
            re.I,
        ),
    ),
    (
        "owner_q",
        re.compile(r"who owns the\s+(?P<title>.+?)\?+", re.I),
    ),
]
_OWNER_SUGGEST = re.compile(r"Owner suggestion:\s*(?P<owner>[A-Z][a-z]+ [A-Z][a-z]+)")


@dataclass
class Intent:
    hint: str
    title: str
    due: str | None
    owner: str | None
    frm: str | None
    quote: str
    tentative: bool = False


def message_text(message) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def source_from_messages(messages) -> str:
    for message in messages:
        text = message_text(message)
        if "SOURCE_TEXT:" not in text:
            continue
        rest = text.split("SOURCE_TEXT:", 1)[1].lstrip("\n")
        if "<<<FIELDNOTE_OBSERVATIONS>>>" in rest:
            rest = rest.split("<<<FIELDNOTE_OBSERVATIONS>>>", 1)[0]
        return rest.strip("\n")
    return ""


def project_id_from_messages(messages) -> str:
    for message in messages:
        match = re.search(r"^PROJECT_ID:\s*(\S+)", message_text(message), re.M)
        if match:
            return match.group(1)
    return ""


def intents_from_source(source: str) -> list[Intent]:
    intents: list[Intent] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if not line:
            continue
        suggestion = _OWNER_SUGGEST.search(line)
        if suggestion and intents and intents[-1].hint == "new" and not intents[-1].owner:
            intents[-1].owner = suggestion.group("owner")
            continue
        for hint, pattern in _PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            groups = match.groupdict()
            title = " ".join(groups.get("title", "").replace("?", " ").split())
            title = title.rstrip(".").strip()
            if not title:
                continue
            intents.append(
                Intent(
                    hint=hint,
                    title=title,
                    due=groups.get("due"),
                    owner=(groups.get("owner") or "").strip() or None,
                    frm=groups.get("frm"),
                    quote=line.rstrip(),
                    tentative=hint == "owner",
                )
            )
            break
    return intents


def _loads(content: str):
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def observations_from_payloads(payloads: list[dict]) -> dict:
    searches: dict[str, list[dict]] = {}
    people: dict[str, list[dict]] = {}
    deps: dict[str, dict] = {}
    for payload in payloads:
        tool = payload.get("tool")
        content = payload.get("content")
        if isinstance(content, str):
            content = _loads(content)
        if not isinstance(content, dict):
            continue
        if tool == "search_tasks":
            searches[normalize(str(content.get("query") or ""))] = content.get("matches") or []
        elif tool == "resolve_person":
            people[normalize(str(content.get("query") or ""))] = content.get("matches") or []
        elif tool == "task_dependencies":
            if content.get("task_id"):
                deps[content["task_id"]] = content
    return {"searches": searches, "people": people, "deps": deps}


def observations_from_messages(messages) -> dict:
    payloads = []
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        payloads.append({"tool": getattr(message, "name", ""), "content": message_text(message)})
    for message in messages:
        text = message_text(message)
        marker = "<<<FIELDNOTE_OBSERVATIONS>>>"
        if marker not in text or "<<<END_OBSERVATIONS>>>" not in text:
            continue
        block = text.split(marker, 1)[1].split("<<<END_OBSERVATIONS>>>", 1)[0]
        parsed = _loads(block.strip())
        if isinstance(parsed, list):
            payloads.extend(parsed)
    return observations_from_payloads(payloads)


def _person(observations: dict, name: str | None) -> dict | None:
    if not name:
        return None
    matches = observations["people"].get(normalize(name), [])
    exact = [item for item in matches if item.get("display_name", "").lower() == name.lower()]
    if len(exact) == 1:
        return exact[0]
    return None


def _deps_sentence(observations: dict, task_id: str | None) -> str:
    if not task_id or task_id not in observations["deps"]:
        return ""
    blocked = [
        item
        for item in observations["deps"][task_id].get("predecessors") or []
        if item.get("status") == "Blocked"
    ]
    if not blocked:
        return ""
    first = blocked[0]
    return (
        f" {first['task_id']} {first['task_title']} is a blocked predecessor"
        f" ({first.get('dependency_type') or 'link'})."
    )


def packet_from_observations(source: str, observations: dict) -> dict:
    proposals = []
    for intent in intents_from_source(source):
        matches = observations["searches"].get(normalize(intent.title), [])
        scored = [
            Scored(
                task_id=item["task_id"],
                title=item.get("title") or "",
                score=float(item.get("score") or 0),
                owner_name=item.get("owner_name") or "",
                planned_due=item.get("planned_due") or "",
                status=item.get("status") or "",
            )
            for item in matches
            if item.get("task_id")
        ]
        decision = decide_link(scored)
        person = _person(observations, intent.owner)
        owner_name = person["display_name"] if person else intent.owner
        task_id = decision.task.task_id if decision.task else None
        plan_bit = ""
        if decision.task:
            plan_bit = (
                f" {decision.task.task_id} {decision.task.title} is the plan row"
                f" ({decision.task.status}, due {decision.task.planned_due}, {decision.task.owner_name or 'no owner'})."
            )
        elif decision.strength == "ambiguous":
            listed = ", ".join(f"{item.task_id} {item.title}" for item in decision.cousins[:4])
            plan_bit = f" Several rows fit and none was chosen: {listed}."
        elif decision.nearby:
            seen_titles = []
            bits = []
            for item in decision.nearby:
                if item.title in seen_titles:
                    continue
                seen_titles.append(item.title)
                bits.append(f"{item.title} ({item.score:.2f})")
                if len(bits) == 2:
                    break
            plan_bit = f" Nearest titles were left untouched: {', '.join(bits)}."

        kind = "conflict"
        status = None
        due = intent.due
        needs_human = True
        confidence = 0.42
        if intent.hint == "new":
            if decision.strength == "exact":
                kind = "conflict"
                rationale = f"The note asks to create {intent.title}, but that title is already on the plan.{plan_bit}"
            else:
                kind = "new_task"
                task_id = None
                confidence = 0.72
                needs_human = decision.strength == "ambiguous"
                rationale = f"The note asks for a new task, {intent.title}, due {due}.{plan_bit}"
        elif intent.hint == "blocked":
            status = "Blocked"
            if task_id and decision.strength == "exact":
                kind = "status_update"
                confidence = 0.9
                needs_human = False
                rationale = f"The note says to mark this blocked.{plan_bit}"
            else:
                rationale = f"The note says to mark {intent.title} blocked, and the plan does not identify one row.{plan_bit}"
        elif intent.hint == "shift":
            if task_id:
                kind = "date_change"
                confidence = 0.86 if decision.strength == "exact" else 0.6
                needs_human = decision.strength != "exact"
                rationale = (
                    f"The note moves the due date from {intent.frm} to {due}.{plan_bit}"
                )
            else:
                rationale = (
                    f"The note moves {intent.title} from {intent.frm} to {due}, "
                    f"but there is no row to move yet.{plan_bit}"
                )
        elif intent.hint in {"assign", "owner"}:
            if task_id and person:
                kind = "owner_change"
                confidence = 0.8 if decision.strength == "exact" else 0.58
                needs_human = True if intent.tentative or decision.strength != "exact" else False
                who = f"{owner_name} ({person['person_id']})"
                rationale = f"The note assigns {who} and the date {due}.{plan_bit}"
                if intent.tentative:
                    rationale = f"The note's owner is marked tentative: {who}, by {due}.{plan_bit}"
            else:
                rationale = f"The note discusses who should update {intent.title}, and it is not settled against the plan.{plan_bit}"
        elif intent.hint == "owner_q":
            rationale = f"The note asks who owns {intent.title} and does not name anyone.{plan_bit}"
        else:
            rationale = f"The note mentions {intent.title}.{plan_bit}"

        rationale += _deps_sentence(observations, task_id if kind != "new_task" else None)
        proposals.append(
            {
                "kind": kind,
                "task_id": task_id if kind != "new_task" else None,
                "title": intent.title if kind == "new_task" else (decision.task.title if decision.task else intent.title),
                "owner_name": owner_name if person else intent.owner,
                "due_date": due,
                "from_date": intent.frm,
                "status": status,
                "evidence_quote": intent.quote,
                "rationale": " ".join(rationale.split()),
                "confidence": confidence,
                "needs_human": needs_human,
            }
        )
    notes = ""
    if not proposals:
        notes = "No line in this source matched a create, shift, block, assignment, or ownership question."
    return ReviewPacket.model_validate({"proposals": proposals, "model_notes": notes}).model_dump()


def tool_plan(source: str, project_id: str) -> list[dict]:
    """First research turn: one search per work item, one lookup per named person."""
    calls = [{"name": "project_risks", "args": {"project_id": project_id}}]
    seen_titles: set[str] = set()
    seen_people: set[str] = set()
    for intent in intents_from_source(source):
        key = normalize(intent.title)
        if key and key not in seen_titles:
            seen_titles.add(key)
            calls.append({"name": "search_tasks", "args": {"project_id": project_id, "query": intent.title}})
            calls.append({"name": "search_sources", "args": {"project_id": project_id, "query": intent.title}})
        if intent.owner and intent.owner not in seen_people:
            seen_people.add(intent.owner)
            calls.append({"name": "resolve_person", "args": {"name": intent.owner}})
    return calls[:16]


def dependency_calls(observations: dict) -> list[dict]:
    calls = []
    for matches in observations["searches"].values():
        scored = [
            Scored(task_id=item["task_id"], title=item.get("title") or "", score=float(item.get("score") or 0))
            for item in matches
            if item.get("task_id")
        ]
        decision = decide_link(scored)
        if decision.strength == "exact" and decision.task:
            calls.append({"name": "task_dependencies", "args": {"task_id": decision.task.task_id}})
    # Unique, stable, and short.
    unique = []
    seen = set()
    for call in calls:
        task_id = call["args"]["task_id"]
        if task_id in seen:
            continue
        seen.add(task_id)
        unique.append(call)
    return unique[:3]
