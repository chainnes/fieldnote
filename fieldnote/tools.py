from __future__ import annotations

import json
from contextvars import ContextVar

from langchain_core.tools import tool

from fieldnote.db import Store

CURRENT_STORE: ContextVar[Store] = ContextVar("fieldnote_store")


def _store() -> Store:
    try:
        return CURRENT_STORE.get()
    except LookupError as exc:
        raise RuntimeError("Fieldnote tools were called without a plan store.") from exc


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


@tool
def search_tasks(project_id: str, query: str) -> str:
    """Search one project's tasks by the wording in the source.

    Use the work item's own phrase as the query. Do not guess a task id first.
    Returns the closest tasks with ids, owners, due dates, status, and match scores.
    A high score is not permission to update the plan.
    """
    matches = []
    for item in _store().search_tasks(project_id, query, limit=8):
        matches.append(
            {
                "task_id": item.task_id,
                "title": item.title,
                "score": item.score,
                "owner_id": item.owner_id,
                "owner_name": item.owner_name,
                "planned_due": item.planned_due,
                "status": item.status,
                "priority": item.priority,
            }
        )
    return _dump({"project_id": project_id, "query": query, "matches": matches})


@tool
def resolve_person(name: str) -> str:
    """Resolve a display name against the people directory.

    Returns zero, one, or several people. Never invent a person id. A single
    given name is not enough when several people share it.
    """
    matches = [
        {
            "person_id": person["person_id"],
            "display_name": person["display_name"],
            "role": person["role"],
            "discipline": person["discipline"],
            "email": person["email"],
        }
        for person in _store().resolve_person(name)
    ]
    return _dump({"query": name, "matches": matches})


@tool
def task_dependencies(task_id: str) -> str:
    """List predecessors and successors for a task id that search_tasks returned."""
    return _dump(_store().dependencies_for(task_id))


@tool
def search_sources(project_id: str, query: str) -> str:
    """Search embedded meeting notes and emails for evidence.

    Uses the local deterministic embedding in mock mode. Returns the closest
    chunks with source ids and scores. Quote only text that came back from this
    tool or from the source the reviewer is reading.
    """
    hits = _store().search_sources(project_id, query, limit=5)
    trimmed = []
    for hit in hits:
        trimmed.append(
            {
                "source_kind": hit["source_kind"],
                "source_id": hit["source_id"],
                "title": hit["title"],
                "score": hit["score"],
                "text": hit["text"][:400],
            }
        )
    return _dump({"project_id": project_id, "query": query, "hits": trimmed})


@tool
def project_risks(project_id: str) -> str:
    """Count overdue work, blocked work, and rows whose current snapshot disagrees with the master plan.

    Uses the desk's planning clock. This is context for the reader, not a source of new tasks.
    """
    return _dump(_store().project_risks(project_id))


TOOLS = [search_tasks, search_sources, resolve_person, task_dependencies, project_risks]
