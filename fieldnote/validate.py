from __future__ import annotations

import re
from typing import Any

from datetime import datetime

from fieldnote.db import pretty_date
from fieldnote.match import Scored, decide_link, quote_in_source
from fieldnote.schemas import STATUSES, ModelProposal

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()[:10]
    return value if _DATE.match(value) else None


def _as_scored(matches: list[dict]) -> list[Scored]:
    scored = []
    for match in matches:
        scored.append(
            Scored(
                task_id=match["task_id"],
                title=match.get("title") or match.get("task_title") or "",
                score=float(match.get("score") or 0),
                owner_id=match.get("owner_id") or "",
                owner_name=match.get("owner_name") or "",
                planned_due=match.get("planned_due") or "",
                status=match.get("status") or "",
                priority=match.get("priority") or "",
            )
        )
    return scored


def _cousin_dicts(items: list[Scored]) -> list[dict]:
    return [
        {"task_id": item.task_id, "title": item.title, "score": round(item.score, 3)}
        for item in items
    ]


def validate_drafts(
    drafts: list[ModelProposal],
    source_text: str,
    store: Any,
    project_id: str,
) -> tuple[list[dict], list[str]]:
    """Drop invented quotes and refuse ambiguous links. Returns stored-proposal dicts."""
    notes: list[str] = []
    prepared: list[dict] = []
    for draft in drafts[:12]:
        if not quote_in_source(draft.evidence_quote, source_text):
            notes.append("Dropped a proposal whose quote is not in the source.")
            continue
        title = " ".join(draft.title.split())
        due = _clean_date(draft.due_date)
        frm = _clean_date(draft.from_date)
        status = draft.status if draft.status in STATUSES else None
        if draft.status and status is None:
            notes.append(f"Cleared an unknown status on “{title}”.")

        owner_name = (draft.owner_name or "").strip() or None
        owner_id = None
        owner_role = ""
        if owner_name:
            found = store.resolve_person(owner_name)
            exact = [person for person in found if person["display_name"].lower() == owner_name.lower()]
            if len(exact) == 1:
                owner_id = exact[0]["person_id"]
                owner_name = exact[0]["display_name"]
                owner_role = exact[0]["role"]
            else:
                notes.append(f"“{owner_name}” is not a single person in the directory.")
                owner_name = None

        scored = store.search_tasks(project_id, title, limit=8)
        decision = decide_link(scored)
        task = None
        if draft.task_id:
            candidate = store.get_task(draft.task_id)
            if not candidate or candidate["project_id"] != project_id:
                notes.append(f"Ignored {draft.task_id}; it is not a task on this project.")
            else:
                similarity = next((item.score for item in scored if item.task_id == draft.task_id), 0)
                title_in_quote = candidate["task_title"].lower() in draft.evidence_quote.lower()
                linked = decision.task is not None and decision.task.task_id == draft.task_id
                if similarity >= 0.5 or title_in_quote or linked:
                    task = candidate
                else:
                    notes.append(
                        f"Ignored {draft.task_id}; it does not match “{title}”."
                    )
        if task is None and decision.task is not None:
            task = store.get_task(decision.task.task_id)

        kind = draft.kind
        conflict_reason = None
        cousins = _cousin_dicts(decision.cousins)
        nearby = _cousin_dicts(decision.nearby)

        if kind == "new_task" and task is not None and decision.strength in {"exact", "fuzzy"}:
            kind = "conflict"
            conflict_reason = (
                f"{task['task_id']} {task['task_title']} is already on the plan, so a second row was not opened."
            )
            cousins = [
                {
                    "task_id": task["task_id"],
                    "title": task["task_title"],
                    "score": 1 if decision.strength == "exact" else 0.84,
                }
            ]
        elif kind != "new_task" and task is None:
            kind = "conflict"
            if decision.strength == "ambiguous":
                conflict_reason = "Several tasks fit this wording. None was selected."
            else:
                conflict_reason = "No task on this project is close enough to update."
                cousins = nearby
        elif decision.strength == "fuzzy" and task is not None:
            conflict_reason = None

        if kind == "owner_change" and not owner_id:
            kind = "conflict"
            conflict_reason = "The note names an owner the directory cannot confirm."

        rationale = (draft.rationale or "").strip()
        if task and task["task_id"] not in rationale:
            extra = (
                f"{task['task_id']} {task['task_title']} is on the plan, "
                f"owned by {task['owner_name'] or 'nobody'}, due {task['planned_due']}, status {task['status']}."
            )
            rationale = f"{rationale} {extra}".strip()
        if frm and task and frm != task["planned_due"]:
            mismatch = (
                f"The note treats the current due date as {frm}. The plan says {task['planned_due']}."
            )
            rationale = f"{rationale} {mismatch}".strip()
            if not conflict_reason and decision.strength != "exact":
                conflict_reason = mismatch
        if nearby and kind == "conflict" and not cousins:
            cousins = nearby
            listed = ", ".join(f"{item['task_id']} {item['title']}" for item in nearby)
            rationale = f"{rationale} Nearest on the plan: {listed}.".strip()

        attention = "review"
        clean_exact = (
            decision.strength == "exact"
            and kind in {"status_update", "date_change", "owner_change"}
            and not draft.needs_human
            and not (frm and task and frm != task["planned_due"])
            and (kind != "owner_change" or owner_id)
        )
        if clean_exact:
            attention = "routine"
        if kind == "conflict":
            attention = "review"

        applicable = False
        if kind == "new_task" and title:
            applicable = True
        elif kind == "status_update" and task and status:
            applicable = status != task["status"]
            if not applicable:
                notes.append(f"Left out {task['task_id']}; it is already {status}.")
                continue
        elif kind == "date_change" and task and due:
            applicable = due != task["planned_due"]
            if not applicable:
                notes.append(f"Left out {task['task_id']}; the due date is already {due}.")
                continue
        elif kind == "owner_change" and task and owner_id:
            applicable = owner_id != task["owner_id"] or (due is not None and due != task["planned_due"])
            if not applicable:
                notes.append(f"Left out {task['task_id']}; the owner is already {owner_name}.")
                continue

        if not rationale:
            rationale = conflict_reason or "Quoted from the source and checked against the plan."
        if owner_role and owner_name and owner_name not in rationale:
            rationale = f"{rationale} {owner_name} is in the directory ({owner_role}).".strip()

        confidence = max(0.0, min(1.0, float(draft.confidence)))
        linked_id = None if kind == "new_task" or task is None else task["task_id"]
        vector_hits = store.similar_tasks(project_id, title, limit=3)
        reconciliation, reconcile_reason, vector_score = _reconcile(kind, decision, vector_hits, task)
        urgency_score, urgency_reason = _urgency(store, task, due, status, linked_id, draft.urgency_note)
        support = ""
        for hit in store.search_sources(project_id, title, limit=3):
            if hit["score"] < 0.55:
                break
            support = f"{hit['source_kind']} {hit['source_id']} ({hit['score']:.2f})"
            break
        prepared.append(
            {
                "kind": kind,
                "task_id": linked_id,
                "title": title,
                "reconciliation": reconciliation,
                "reconcile_reason": reconcile_reason,
                "vector_score": vector_score,
                "urgency_score": urgency_score,
                "urgency_reason": urgency_reason,
                "support_source": support,
                "off_schema": False,
                "owner_id": owner_id,
                "owner_name": owner_name,
                "due_date": due,
                "from_date": frm,
                "status": status,
                "evidence_quote": draft.evidence_quote.strip(),
                "rationale": rationale[:800],
                "confidence": confidence,
                "attention": attention,
                "conflict_reason": conflict_reason,
                "cousins": cousins,
                "applicable": applicable,
            }
        )

    deduped: list[dict] = []
    seen: set[tuple] = set()
    for item in prepared:
        key = (item["kind"], item.get("task_id"), item["title"].lower(), item.get("due_date"), item.get("status"), item.get("owner_id"))
        if key in seen:
            notes.append(f"Combined a duplicate proposal for {item['title']}.")
            continue
        seen.add(key)
        deduped.append(item)
    return deduped, notes


def _reconcile(kind: str, decision, vector_hits: list[dict], task: dict | None) -> tuple[str, str, float]:
    """Label new_task, update_existing, or conflict_or_ambiguous from both signals."""
    top = vector_hits[0] if vector_hits else None
    vector_score = float(top["score"]) if top else 0.0
    vector_bit = (
        f"Vector similarity {vector_score:.2f} against {top['task_id']} {top['title']}."
        if top
        else "Vector similarity found nothing nearby."
    )
    fuzzy_bit = f"Fuzzy title match is {decision.strength}."
    same_row = bool(top and task and top["task_id"] == task["task_id"])
    if kind == "new_task":
        if top and vector_score >= 0.82 and not same_row:
            return (
                "conflict_or_ambiguous",
                f"{fuzzy_bit} {vector_bit} The wording is close to an existing row, so it is not opened as new.",
                vector_score,
            )
        return (
            "new_task",
            f"{fuzzy_bit} {vector_bit} No existing row is close on both signals.",
            vector_score,
        )
    if kind == "conflict" or decision.strength in {"ambiguous", "none"}:
        return (
            "conflict_or_ambiguous",
            f"{fuzzy_bit} {vector_bit} The signals do not name a single row.",
            vector_score,
        )
    if decision.strength == "exact" or (same_row and vector_score >= 0.55):
        return (
            "update_existing",
            f"{fuzzy_bit} {vector_bit} Both signals point at the same row.",
            vector_score,
        )
    return (
        "conflict_or_ambiguous",
        f"{fuzzy_bit} {vector_bit} Fuzzy and vector disagree, so this stays unresolved.",
        vector_score,
    )


def _urgency(store, task: dict | None, due: str | None, status: str | None, task_id: str | None, model_note: str) -> tuple[int, str]:
    score = 0
    parts: list[str] = []
    due_value = due or (task or {}).get("planned_due")
    parsed = None
    if due_value:
        try:
            parsed = datetime.strptime(str(due_value)[:10], "%Y-%m-%d").date()
        except ValueError:
            parsed = None
    if parsed is not None:
        delta = (parsed - store.as_of).days
        if delta < 0:
            score += 40
            parts.append(f"overdue by {-delta} days")
        elif delta <= 7:
            score += 30
            parts.append(f"due in {delta} days")
        elif delta <= 21:
            score += 15
            parts.append(f"due in {delta} days")
    if status == "Blocked" or (task and task.get("status") == "Blocked"):
        score += 30
        parts.append("blocked")
    fanout = store.successor_count(task_id) if task_id else 0
    if fanout:
        score += min(30, fanout * 10)
        parts.append(f"{fanout} successor{'s' if fanout != 1 else ''}")
    score = min(100, score)
    rule = "Urgency " + str(score) + " because " + (", ".join(parts) if parts else "there is no due-date, blocker, or dependency pressure") + "."
    note = (model_note or "").strip()
    if note:
        rule = f"{rule} {note}"
    return score, rule[:400]


def describe_change(before: dict | None, after: dict) -> str:
    if before is None:
        owner = after.get("owner_name") or "unassigned"
        return (
            f"Opened {after['task_id']} {after['task_title']}, "
            f"owner {owner}, due {pretty_date(after.get('planned_due'))}, status {after.get('status')}."
        )
    bits = []
    if before.get("status") != after.get("status"):
        bits.append(f"status {before.get('status')} → {after.get('status')}")
    if before.get("planned_due") != after.get("planned_due"):
        bits.append(f"due {pretty_date(before.get('planned_due'))} → {pretty_date(after.get('planned_due'))}")
    if before.get("owner_name") != after.get("owner_name"):
        bits.append(f"owner {before.get('owner_name') or '—'} → {after.get('owner_name') or '—'}")
    if before.get("task_title") != after.get("task_title"):
        bits.append(f"title “{before.get('task_title')}” → “{after.get('task_title')}”")
    if not bits:
        return f"{after['task_id']} already matched this proposal. Nothing on the row changed."
    return f"{after['task_id']} {after['task_title']}: " + "; ".join(bits) + "."
