from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from fieldnote.config import Settings
from fieldnote.db import Store, dedupe_hash, pretty_date, utc_now
from fieldnote.graph import CURRENT_SETTINGS, GRAPH, preamble, tool_trace
from fieldnote.llm import RESEARCH_SYSTEM
from fieldnote.match import normalize
from fieldnote.notify import notify_draft_ready
from fieldnote.schemas import STATUSES
from fieldnote.tools import CURRENT_STORE
from fieldnote.validate import describe_change


class FieldnoteError(Exception):
    pass


class Desk:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def draft(self, project_id: str, kind: str, source_id: str) -> dict:
        project = self.store.get_project(project_id)
        source = self.store.get_source(kind, source_id)
        if not project or not source or source["project_id"] != project_id:
            raise FieldnoteError("That note is not on this project.")
        self._require_provider()
        superseded = self.store.supersede_pending(project_id, kind, source_id)
        state = {
            "messages": [
                SystemMessage(content=RESEARCH_SYSTEM),
                HumanMessage(content=preamble(project, source)),
            ],
            "project_id": project_id,
            "source_text": source["text"],
            "tool_rounds": 0,
        }
        store_token = CURRENT_STORE.set(self.store)
        settings_token = CURRENT_SETTINGS.set(self.settings)
        try:
            final = GRAPH.invoke(state, config={"recursion_limit": 20})
        except Exception as exc:
            raise FieldnoteError(f"The research step failed ({exc}).") from exc
        finally:
            CURRENT_STORE.reset(store_token)
            CURRENT_SETTINGS.reset(settings_token)

        notes = list(final.get("notes") or [])
        if superseded:
            notes.insert(0, "An earlier unread draft from this note was set aside.")
        batch_id = self.store.next_id("batches", "batch_id", "B-")
        trace = tool_trace(final.get("messages") or [])
        self.store.insert_batch(
            {
                "batch_id": batch_id,
                "project_id": project_id,
                "source_kind": kind,
                "source_id": source_id,
                "provider": self.settings.provider,
                "model": self.settings.model if self.settings.provider != "mock" else "fieldnote-rules",
                "tool_trace_json": json.dumps(trace),
                "notes": "\n".join(note for note in notes if note),
                "created_at": utc_now(),
            }
        )
        for proposal in final.get("proposals") or []:
            proposal["proposal_id"] = self.store.next_id("proposals", "proposal_id", "R-")
            proposal["batch_id"] = batch_id
            proposal["project_id"] = project_id
            proposal["source_kind"] = kind
            proposal["source_id"] = source_id
            proposal["created_at"] = utc_now()
            self.store.insert_proposal(proposal)
        if final.get("off_schema"):
            self.queue_off_schema(project_id, kind, source_id, final.get("off_schema_error") or "", batch_id)
        notice = notify_draft_ready(
            self.settings.n8n_webhook_url,
            {
                "event": "draft_ready",
                "project_id": project_id,
                "source_kind": kind,
                "source_id": source_id,
                "batch_id": batch_id,
                "pending": self.store.count_pending(),
            },
        )
        self.store.conn.execute(
            "UPDATE batches SET notes = ? WHERE batch_id = ?",
            ("\n".join(line for line in [notes and "\n".join(n for n in notes if n), f"n8n: {notice}"] if line), batch_id),
        )
        self.store.conn.commit()
        return self.store.latest_batch(project_id, kind, source_id) or {"batch_id": batch_id}

    def queue_off_schema(self, project_id: str, kind: str, source_id: str, error: str, batch_id: str | None = None) -> str:
        """Park one unreadable model output on the review queue."""
        proposal_id = self.store.next_id("proposals", "proposal_id", "R-")
        self.store.insert_proposal(
            {
                "proposal_id": proposal_id,
                "batch_id": batch_id or "",
                "project_id": project_id,
                "source_kind": kind,
                "source_id": source_id,
                "kind": "conflict",
                "title": "Model output needs a person",
                "evidence_quote": "Off-schema model output could not be repaired.",
                "rationale": (error or "The model output did not match the schema after one repair.")[:800],
                "reconciliation": "conflict_or_ambiguous",
                "reconcile_reason": "Off-schema output went through one repair and still failed, so it is on the review queue.",
                "attention": "review",
                "applicable": False,
                "off_schema": True,
                "confidence": 0,
                "created_at": utc_now(),
            }
        )
        return proposal_id

    def _require_provider(self) -> None:
        provider = self.settings.provider
        if provider in {"openai", "anthropic"} and not self.settings.api_key:
            raise FieldnoteError(
                "LLM_API_KEY is not set. Stay on LLM_PROVIDER=mock, or put a key in .env (it is not committed)."
            )
        if provider == "compatible" and not self.settings.base_url:
            raise FieldnoteError("LLM_BASE_URL is required when LLM_PROVIDER=compatible.")
        if provider not in {"mock", "openai", "anthropic", "compatible"}:
            raise FieldnoteError(
                f"Unknown LLM_PROVIDER {provider!r}. Use mock, openai, anthropic, or compatible."
            )

    def approve(self, proposal_id: str, form: dict) -> str:
        proposal = self.store.get_proposal(proposal_id)
        if not proposal or proposal["review_status"] != "pending":
            raise FieldnoteError("That proposal is no longer waiting.")
        force = (form.get("force") or "") == "yes"
        if not proposal["applicable"] and not force:
            raise FieldnoteError("This one needs a resolution before it can be written. Pick a task, or create a new one.")

        due = _picked(form, "due_date", proposal.get("due_date"))
        status = _picked(form, "status", proposal.get("status"))
        title = _picked(form, "title", proposal.get("title")) or proposal.get("title")
        owner_id = _picked(form, "owner_id", proposal.get("owner_id"))
        if "task_id" in form:
            chosen_task = _picked(form, "task_id", None)
        else:
            chosen_task = proposal.get("task_id")

        if status and status not in STATUSES:
            raise FieldnoteError(f"Status must be one of: {', '.join(STATUSES)}.")
        if due:
            due = due.strip()[:10]
            if len(due) != 10 or due[4] != "-" or due[7] != "-":
                raise FieldnoteError("Due date must be YYYY-MM-DD.")
        owner = self.store.get_person(owner_id) if owner_id else None
        if owner_id and not owner:
            raise FieldnoteError("That owner is not in the directory.")

        create_new = proposal["kind"] == "new_task" or (force and not chosen_task)
        if create_new:
            subject = "title:" + normalize(title or "untitled")
            planned = {
                "task_title": title or "Untitled task",
                "status": status or "Not started",
                "planned_due": due or "",
                "owner_name": owner["display_name"] if owner else "",
            }
            fresh = {
                field: value
                for field, value in planned.items()
                if not self._already_logged(proposal, subject, field, value)
            }
            if not fresh:
                summary = f"“{title}” was already written from an earlier approval. No second row was opened."
                task_id = None
                before = None
                after = None
            else:
                task_id = self.store.next_id("tasks", "task_id", "N")
                source = self.store.get_source(proposal["source_kind"], proposal["source_id"])
                start = (source["when"][:10] if source and source.get("when") else self.store.as_of.isoformat())
                row = {
                    "task_id": task_id,
                    "project_id": proposal["project_id"],
                    "task_title": planned["task_title"],
                    "discipline": "General",
                    "owner_id": owner["person_id"] if owner else "",
                    "owner_name": planned["owner_name"],
                    "planned_start": start,
                    "planned_due": planned["planned_due"],
                    "status": planned["status"],
                    "percent_complete": 100 if planned["status"] == "Done" else 0,
                    "priority": "Medium",
                    "notes": f"Opened from {proposal['source_id']} after approval.",
                    "origin": "approved",
                }
                self.store.insert_task(row)
                for field, value in fresh.items():
                    self._log_field(proposal, subject, task_id, field, "", value)
                summary = describe_change(None, row)
                after = row
                before = None
        else:
            if not chosen_task:
                raise FieldnoteError("Choose the task this should update, or create a new one.")
            before = self.store.get_task(chosen_task)
            if not before or before["project_id"] != proposal["project_id"]:
                raise FieldnoteError("That task is not on this project.")
            desired = {}
            if title and proposal["kind"] == "new_task":
                desired["task_title"] = title
            if status and status != before["status"]:
                desired["status"] = status
            if due and due != before["planned_due"]:
                desired["planned_due"] = due
            if owner and owner["person_id"] != before["owner_id"]:
                desired["owner_id"] = owner["person_id"]
                desired["owner_name"] = owner["display_name"]
            fields = {}
            for field, value in desired.items():
                if self._already_logged(proposal, chosen_task, field, value):
                    continue
                if self._log_field(proposal, chosen_task, chosen_task, field, before.get(field) or "", value):
                    fields[field] = value
            if fields.get("status") == "Done":
                fields["percent_complete"] = 100
            if fields:
                self.store.update_task(chosen_task, fields)
            after = self.store.get_task(chosen_task)
            summary = describe_change(before, after)
            task_id = chosen_task

        note = (form.get("note") or "").strip()
        self.store.mark_proposal(proposal_id, "approved", note)
        self.store.add_audit(
            {
                "audit_id": self.store.next_id("audit", "audit_id", "L-"),
                "created_at": utc_now(),
                "action": "approved",
                "proposal_id": proposal_id,
                "project_id": proposal["project_id"],
                "source_kind": proposal["source_kind"],
                "source_id": proposal["source_id"],
                "task_id": task_id,
                "summary": summary,
                "evidence_quote": proposal["evidence_quote"],
                "before": _public_task(before) if before else None,
                "after": _public_task(after),
            }
        )
        return summary

    def reject(self, proposal_id: str, reason: str) -> None:
        proposal = self.store.get_proposal(proposal_id)
        if not proposal or proposal["review_status"] != "pending":
            raise FieldnoteError("That proposal is no longer waiting.")
        reason = (reason or "").strip() or "Left the plan as it is."
        self.store.mark_proposal(proposal_id, "rejected", reason)
        self.store.add_audit(
            {
                "audit_id": self.store.next_id("audit", "audit_id", "L-"),
                "created_at": utc_now(),
                "action": "rejected",
                "proposal_id": proposal_id,
                "project_id": proposal["project_id"],
                "source_kind": proposal["source_kind"],
                "source_id": proposal["source_id"],
                "task_id": proposal.get("task_id"),
                "summary": f"Left the plan. {reason}",
                "evidence_quote": proposal["evidence_quote"],
                "before": None,
                "after": None,
            }
        )

    def present(self, proposal: dict) -> dict:
        task = self.store.get_task(proposal["task_id"]) if proposal.get("task_id") else None
        project = self.store.get_project(proposal["project_id"])
        batch = self.store._one("SELECT * FROM batches WHERE batch_id = ?", (proposal["batch_id"],))
        provider = (batch or {}).get("provider") or "mock"
        view = dict(proposal)
        view["project_name"] = project["project_name"] if project else proposal["project_id"]
        view["before"] = _public_task(task) if task else None
        view["plan_title"] = task["task_title"] if task else None
        view["confidence_label"] = "Rule confidence" if provider == "mock" else "Model confidence"
        view["confidence_pct"] = round(float(proposal.get("confidence") or 0) * 100)
        view["provider"] = provider
        return view

    def present_many(self, proposals: list[dict]) -> list[dict]:
        return [self.present(proposal) for proposal in proposals]


    def _already_logged(self, proposal: dict, subject: str, field: str, value: str) -> bool:
        digest = dedupe_hash(proposal["project_id"], subject, field, value or "")
        row = self.store.conn.execute(
            "SELECT 1 FROM change_log WHERE dedupe_hash = ?",
            (digest,),
        ).fetchone()
        return row is not None

    def _log_field(self, proposal: dict, subject: str, task_id: str, field: str, old: str, new: str) -> bool:
        return self.store.record_change(
            {
                "change_id": self.store.next_id("change_log", "change_id", "C-"),
                "proposal_id": proposal["proposal_id"],
                "project_id": proposal["project_id"],
                "subject": subject,
                "task_id": task_id,
                "field_name": field,
                "old_value": old or "",
                "new_value": new or "",
                "source_kind": proposal.get("source_kind"),
                "source_id": proposal.get("source_id"),
            }
        )


def _picked(form: dict, key: str, stored):
    """Missing field keeps the stored value. A blank field clears it."""
    if key not in form:
        return stored or None
    text = str(form.get(key) or "").strip()
    return text or None


def _public_task(task: dict | None) -> dict | None:
    if not task:
        return None
    return {
        "task_id": task["task_id"],
        "task_title": task["task_title"],
        "owner_name": task.get("owner_name") or "",
        "planned_due": task.get("planned_due") or "",
        "status": task.get("status") or "",
        "planned_due_label": pretty_date(task.get("planned_due")),
    }
