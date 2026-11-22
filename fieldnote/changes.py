"""Diff the baseline snapshot against the current snapshot."""

from __future__ import annotations

from datetime import date, datetime, timedelta


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _parse(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def detect_changes(store, shift_days: int = 3) -> list[dict]:
    """Flag date shifts, owner changes, new blockers, and scope additions.

    The pack's snapshots are not dated, so a row is a difference between
    baseline and current, not a claim that it happened on a particular day.
    """
    flags: list[dict] = []
    projects = {row["project_id"]: row["project_name"] for row in store.list_projects()}
    baseline = {
        row["task_id"]: row
        for row in store._all("SELECT * FROM snapshots WHERE snapshot = 'baseline'")
    }
    current = {
        row["task_id"]: row
        for row in store._all("SELECT * FROM snapshots WHERE snapshot = 'current'")
    }
    live = {row["task_id"]: row for row in store._all("SELECT * FROM tasks")}

    for task_id, cur in current.items():
        base = baseline.get(task_id)
        project_id = cur["project_id"]
        title = cur["task_title"]
        if base is None:
            flags.append(
                _flag(project_id, projects, task_id, title, "scope_addition", "In the current snapshot only.", "", title, cur.get("planned_due"))
            )
            continue
        old_due, new_due = _parse(base.get("planned_due")), _parse(cur.get("planned_due"))
        if old_due and new_due:
            delta = abs((new_due - old_due).days)
            if delta > shift_days:
                flags.append(
                    _flag(
                        project_id,
                        projects,
                        task_id,
                        title,
                        "date_shift",
                        f"Due date moved {delta} days (threshold {shift_days}).",
                        base.get("planned_due") or "",
                        cur.get("planned_due") or "",
                        cur.get("planned_due"),
                    )
                )
        if (base.get("owner_name") or "") != (cur.get("owner_name") or ""):
            flags.append(
                _flag(
                    project_id,
                    projects,
                    task_id,
                    title,
                    "owner_change",
                    "Owner differs between baseline and the current snapshot.",
                    base.get("owner_name") or "—",
                    cur.get("owner_name") or "—",
                    cur.get("planned_due"),
                )
            )
        if base.get("status") != "Blocked" and cur.get("status") == "Blocked":
            flags.append(
                _flag(
                    project_id,
                    projects,
                    task_id,
                    title,
                    "new_blocker",
                    "Blocked on the current snapshot and not on the baseline.",
                    base.get("status") or "",
                    "Blocked",
                    cur.get("planned_due"),
                )
            )

    for task_id, task in live.items():
        if task_id in baseline:
            continue
        flags.append(
            _flag(
                task["project_id"],
                projects,
                task_id,
                task["task_title"],
                "scope_addition",
                "Opened on this desk. It is not in the baseline snapshot.",
                "",
                task["task_title"],
                task.get("planned_due"),
            )
        )
    flags.sort(key=lambda item: (item["project_id"], item["change_type"], item["task_id"]))
    return flags


def _flag(project_id, projects, task_id, title, change_type, detail, old, new, due) -> dict:
    return {
        "project_id": project_id,
        "project_name": projects.get(project_id, project_id),
        "task_id": task_id,
        "title": title,
        "change_type": change_type,
        "detail": detail,
        "old_value": old,
        "new_value": new,
        "due": due or "",
    }


def in_week(flags: list[dict], day: date) -> list[dict]:
    start, end = week_bounds(day)
    chosen = []
    for flag in flags:
        due = _parse(flag.get("due"))
        if due and start <= due <= end:
            chosen.append(flag)
        elif flag["change_type"] == "scope_addition":
            chosen.append(flag)
    return chosen
