from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from fieldnote.changes import detect_changes, in_week, week_bounds
from fieldnote.config import Settings
from fieldnote.db import Store, pretty_date
from fieldnote.schemas import KIND_LABEL, RECONCILE_LABEL, STATUSES
from fieldnote.service import Desk, FieldnoteError

PACKAGE = Path(__file__).resolve().parent


def pretty_when(value: str | None) -> str:
    if not value:
        return "—"
    day = pretty_date(value[:10])
    if "T" in value and len(value) >= 16:
        return f"{day}, {value[11:16]}"
    return day


def highlight(text: str, quotes: list[str] | None) -> Markup:
    escaped = str(escape(text or ""))
    used: list[str] = []
    for quote in sorted({q.strip() for q in (quotes or []) if q and q.strip()}, key=len, reverse=True):
        if any(quote in earlier for earlier in used):
            continue
        needle = str(escape(quote))
        if needle not in escaped:
            continue
        escaped = escaped.replace(needle, f"<mark>{needle}</mark>")
        used.append(quote)
    return Markup(escaped)


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or Store(settings.db_path, settings.seed_dir, settings.as_of)
    app = FastAPI(title="Fieldnote", docs_url=None, redoc_url=None)
    app.state.desk = Desk(settings, store)
    templates = Jinja2Templates(directory=str(PACKAGE / "templates"))
    templates.env.filters["pretty_date"] = pretty_date
    templates.env.filters["pretty_when"] = pretty_when
    templates.env.filters["highlight"] = highlight
    templates.env.globals["kind_label"] = KIND_LABEL
    templates.env.globals["reconcile_label"] = RECONCILE_LABEL
    templates.env.globals["statuses"] = STATUSES
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(PACKAGE / "static")), name="static")

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        desk: Desk = request.app.state.desk
        ctx.setdefault("pending_count", desk.store.count_pending())
        ctx.setdefault("settings", desk.settings)
        ctx.setdefault("notice", request.query_params.get("notice", ""))
        ctx.setdefault("error", request.query_params.get("error", ""))
        context = {"request": request, **ctx}
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    @app.get("/health")
    def health():
        desk: Desk = app.state.desk
        counts = desk.store.counts()
        return {
            "ok": True,
            "provider": desk.settings.provider,
            "as_of": desk.settings.as_of.isoformat(),
            "projects": counts["projects"],
            "tasks": counts["tasks"],
            "pending": desk.store.count_pending(),
        }

    @app.get("/")
    def home(request: Request):
        desk: Desk = request.app.state.desk
        return render(request, "home.html", projects=desk.store.list_projects())

    @app.post("/reset")
    def reset(request: Request):
        request.app.state.desk.store.reset()
        return RedirectResponse("/?notice=The seed plan is restored. Approvals were cleared.", status_code=303)

    @app.get("/p/{project_id}")
    def project(request: Request, project_id: str):
        desk: Desk = request.app.state.desk
        found = desk.store.get_project(project_id)
        if not found:
            return render(request, "missing.html", status_code=404, title="No such project")
        tasks = desk.store.list_tasks(project_id)
        tasks.sort(key=lambda task: (
            0 if task["status"] == "Blocked" else 1 if task["overdue"] else 2 if task["status"] != "Done" else 3,
            task["planned_due"] or "",
            task["task_id"],
        ))
        sources = desk.store.list_sources(project_id)
        return render(
            request,
            "project.html",
            project=found,
            tasks=tasks,
            meetings=sources["meetings"],
            emails=sources["emails"],
        )

    @app.get("/p/{project_id}/s/{kind}/{source_id}")
    def source_page(request: Request, project_id: str, kind: str, source_id: str):
        desk: Desk = request.app.state.desk
        found = desk.store.get_project(project_id)
        source = desk.store.get_source(kind, source_id) if kind in {"meeting", "email"} else None
        if not found or not source or source["project_id"] != project_id:
            return render(request, "missing.html", status_code=404, title="No such note")
        return render(request, "source.html", **_source_context(desk, found, source))

    @app.post("/p/{project_id}/s/{kind}/{source_id}/draft")
    def draft(request: Request, project_id: str, kind: str, source_id: str):
        desk: Desk = request.app.state.desk
        try:
            desk.draft(project_id, kind, source_id)
        except FieldnoteError as exc:
            return RedirectResponse(
                f"/p/{project_id}/s/{kind}/{source_id}?error={_q(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(
            f"/p/{project_id}/s/{kind}/{source_id}?notice=Draft ready. Nothing has been written to the plan.",
            status_code=303,
        )

    @app.get("/queue")
    def queue(request: Request):
        desk: Desk = request.app.state.desk
        pending = desk.present_many(desk.store.list_proposals(status="pending"))
        pending.sort(key=lambda item: (-int(item.get("urgency_score") or 0), item["proposal_id"]))
        people = desk.store.list_people()
        tasks_by_project = {
            project["project_id"]: desk.store.list_tasks(project["project_id"])
            for project in desk.store.list_projects()
        }
        return render(
            request,
            "queue.html",
            proposals=pending,
            people=people,
            tasks_by_project=tasks_by_project,
        )

    @app.get("/changes")
    def changes(request: Request):
        desk: Desk = request.app.state.desk
        flags = detect_changes(desk.store, desk.settings.change_shift_days)
        start, end = week_bounds(desk.settings.as_of)
        return render(
            request,
            "changes.html",
            flags=flags,
            week_flags=in_week(flags, desk.settings.as_of),
            week_start=start,
            week_end=end,
            shift_days=desk.settings.change_shift_days,
        )

    @app.get("/audit")
    def audit(request: Request):
        desk: Desk = request.app.state.desk
        return render(
            request,
            "audit.html",
            rows=desk.store.list_audit(),
            changes=desk.store.list_changes(),
        )

    @app.post("/proposals/{proposal_id}/approve")
    async def approve(request: Request, proposal_id: str):
        desk: Desk = request.app.state.desk
        form = dict(await request.form())
        proposal = desk.store.get_proposal(proposal_id)
        target = _return_to(proposal)
        try:
            summary = desk.approve(proposal_id, form)
        except FieldnoteError as exc:
            return RedirectResponse(f"{target}?error={_q(str(exc))}", status_code=303)
        return RedirectResponse(f"{target}?notice={_q(summary)}", status_code=303)

    @app.post("/proposals/{proposal_id}/reject")
    async def reject(request: Request, proposal_id: str):
        desk: Desk = request.app.state.desk
        form = dict(await request.form())
        proposal = desk.store.get_proposal(proposal_id)
        target = _return_to(proposal)
        try:
            desk.reject(proposal_id, str(form.get("reason") or ""))
        except FieldnoteError as exc:
            return RedirectResponse(f"{target}?error={_q(str(exc))}", status_code=303)
        return RedirectResponse(f"{target}?notice=Left the plan unchanged.", status_code=303)

    return app


def _q(text: str) -> str:
    from urllib.parse import quote

    return quote(text[:240])


def _return_to(proposal: dict | None) -> str:
    if not proposal:
        return "/queue"
    return f"/p/{proposal['project_id']}/s/{proposal['source_kind']}/{proposal['source_id']}"


def _source_context(desk: Desk, project: dict, source: dict) -> dict:
    proposals = desk.present_many(
        desk.store.list_proposals(project_id=project["project_id"], source_id=source["source_id"])
    )
    visible = [item for item in proposals if item["review_status"] != "superseded"]
    batch = desk.store.latest_batch(project["project_id"], source["kind"], source["source_id"])
    trace = []
    batch_notes = ""
    if batch:
        try:
            trace = json.loads(batch["tool_trace_json"] or "[]")
        except json.JSONDecodeError:
            trace = []
        batch_notes = batch.get("notes") or ""
    clock = desk.settings.as_of.isoformat()
    return {
        "project": project,
        "source": source,
        "after_clock": bool(source.get("when")) and source["when"][:10] > clock,
        "pending": [item for item in visible if item["review_status"] == "pending"],
        "decided": [item for item in visible if item["review_status"] != "pending"],
        "quotes": [item["evidence_quote"] for item in visible],
        "trace": trace,
        "batch": batch,
        "batch_notes": [line for line in batch_notes.splitlines() if line.strip()],
        "people": desk.store.list_people(),
        "tasks": desk.store.list_tasks(project["project_id"]),
    }
