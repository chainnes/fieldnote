from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["new_task", "status_update", "date_change", "owner_change", "conflict"]
Attention = Literal["routine", "review"]
STATUSES = ("Not started", "In progress", "Blocked", "Done")


class ModelProposal(BaseModel):
    """What a model is allowed to return. The validator fills the rest."""

    model_config = ConfigDict(extra="ignore")

    kind: Kind
    task_id: str | None = None
    title: str = Field(min_length=2, max_length=180)
    owner_name: str | None = None
    due_date: str | None = None
    from_date: str | None = None
    status: str | None = None
    evidence_quote: str = Field(min_length=8, max_length=500)
    rationale: str = Field(default="", max_length=800)
    confidence: float = 0.5
    needs_human: bool = True
    urgency_note: str = ""


class ReviewPacket(BaseModel):
    """Schema-validated model output for one source."""

    model_config = ConfigDict(extra="ignore")

    proposals: list[ModelProposal] = Field(default_factory=list)
    model_notes: str = ""


def coerce_packet(payload) -> ReviewPacket:
    """Keep valid proposals when a model mixes in a row that fails the schema."""
    if isinstance(payload, ReviewPacket):
        data = payload.model_dump()
    elif isinstance(payload, BaseModel):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = payload
    else:
        raise TypeError("Proposal packet was not an object.")
    kept: list[ModelProposal] = []
    for item in (data.get("proposals") or [])[:12]:
        try:
            kept.append(ModelProposal.model_validate(item))
        except Exception:
            continue
    notes = str(data.get("model_notes") or "")[:500]
    return ReviewPacket(proposals=kept, model_notes=notes)


KIND_LABEL = {
    "new_task": "New task",
    "status_update": "Status",
    "date_change": "Due date",
    "owner_change": "Owner",
    "conflict": "Needs a person",
}

RECONCILE_LABEL = {
    "new_task": "New task",
    "update_existing": "Update existing",
    "conflict_or_ambiguous": "Conflict or ambiguous",
}
