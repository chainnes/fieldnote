from fieldnote.schemas import ModelProposal
from fieldnote.validate import validate_drafts

SOURCE = """Meeting Summary
Key updates:
- Mark CDE folder structure as blocked pending missing survey
- Aisha Ong to update weekly update by 2026-04-03
"""


def _draft(**kwargs):
    base = dict(
        kind="status_update",
        title="CDE folder structure",
        evidence_quote="Mark CDE folder structure as blocked pending missing survey",
        status="Blocked",
        confidence=0.9,
        needs_human=False,
        rationale="Quoted from the note.",
    )
    base.update(kwargs)
    return ModelProposal(**base)


def test_drops_a_quote_that_is_not_in_the_source(store):
    kept, notes = validate_drafts(
        [_draft(evidence_quote="This sentence was never in the note at all")],
        SOURCE,
        store,
        "PRJ001",
    )
    assert kept == []
    assert any("not in the source" in note for note in notes)


def test_links_the_only_cde_row_and_refuses_a_foreign_task_id(store):
    other = next(task for task in store.list_tasks("PRJ002"))
    kept, notes = validate_drafts(
        [_draft(task_id=other["task_id"])],
        SOURCE,
        store,
        "PRJ001",
    )
    assert len(kept) == 1
    assert kept[0]["task_id"] == "T00017"
    assert kept[0]["kind"] == "status_update"
    assert kept[0]["applicable"] is True
    assert kept[0]["attention"] == "routine"
    assert any("Ignored" in note for note in notes)


def test_unknown_owner_becomes_a_conflict(store):
    kept, _notes = validate_drafts(
        [
            _draft(
                kind="owner_change",
                title="CDE folder structure",
                owner_name="Nadia Zap",
                status=None,
                evidence_quote="Aisha Ong to update weekly update by 2026-04-03",
            )
        ],
        SOURCE,
        store,
        "PRJ001",
    )
    assert kept[0]["kind"] == "conflict"
    assert kept[0]["owner_id"] is None
    assert kept[0]["applicable"] is False


def test_duplicate_new_task_title_is_not_opened_twice(store):
    kept, _notes = validate_drafts(
        [
            _draft(
                kind="new_task",
                title="CDE folder structure",
                status=None,
                needs_human=True,
            )
        ],
        SOURCE,
        store,
        "PRJ001",
    )
    assert kept[0]["kind"] == "conflict"
    assert kept[0]["applicable"] is False
    assert kept[0]["task_id"] == "T00017"
