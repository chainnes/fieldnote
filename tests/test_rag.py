import json
from pathlib import Path

from fieldnote.embed import cosine, embed
from fieldnote.notify import notify_draft_ready
from fieldnote.repair import run_with_repair
from fieldnote.tools import CURRENT_STORE, search_sources


def test_embedding_is_stable_and_identical_text_scores_one():
    first = embed("CDE folder structure")
    second = embed("CDE folder structure")
    assert first == second
    assert cosine(first, second) == 1


def test_search_sources_finds_the_meeting_that_mentions_cde(store):
    token = CURRENT_STORE.set(store)
    try:
        payload = json.loads(search_sources.invoke({"project_id": "PRJ001", "query": "CDE folder structure"}))
    finally:
        CURRENT_STORE.reset(token)
    assert payload["hits"]
    assert any(hit["source_id"] == "M0003" for hit in payload["hits"])
    assert store.conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 72 + 160
    assert store.conn.execute("SELECT COUNT(*) FROM project_members").fetchone()[0] == 4 * 18


def test_reconcile_labels_and_urgency(desk, store):
    desk.draft("PRJ001", "meeting", "M0003")
    proposals = store.list_proposals(source_id="M0003", status="pending")
    by_quote = {item["evidence_quote"]: item for item in proposals}
    blocked = by_quote["Mark CDE folder structure as blocked pending missing survey"]
    assert blocked["reconciliation"] == "update_existing"
    assert "Fuzzy" in blocked["reconcile_reason"]
    assert "Vector" in blocked["reconcile_reason"]
    assert blocked["urgency_score"] >= 70
    assert "blocked" in blocked["urgency_reason"]

    created = next(item for item in proposals if item["evidence_quote"].startswith("Create new task: risk register"))
    assert created["reconciliation"] == "new_task"
    assert created["urgency_score"] < blocked["urgency_score"]

    weekly = next(item for item in proposals if "weekly update" in item["evidence_quote"])
    assert weekly["reconciliation"] == "conflict_or_ambiguous"

    trace = json.loads(store.latest_batch("PRJ001", "meeting", "M0003")["tool_trace_json"])
    assert "search_sources" in [call["tool"] for call in trace]
    assert "n8n: skipped" in store.latest_batch("PRJ001", "meeting", "M0003")["notes"]


def test_what_changed_page_lists_a_date_shift(client, store):
    page = client.get("/changes")
    assert page.status_code == 200
    assert "date_shift" in page.text
    assert "What changed" in page.text
    from fieldnote.changes import detect_changes

    flags = detect_changes(store, shift_days=3)
    assert any(flag["change_type"] == "date_shift" for flag in flags)
    assert all(
        abs(
            __import__("datetime").datetime.strptime(flag["new_value"], "%Y-%m-%d")
            - __import__("datetime").datetime.strptime(flag["old_value"], "%Y-%m-%d")
        ).days
        > 3
        for flag in flags
        if flag["change_type"] == "date_shift"
    )


def test_change_log_is_idempotent(client, store):
    client.post("/p/PRJ001/s/meeting/M0003/draft")
    pending = store.list_proposals(source_id="M0003", status="pending")
    blocked = next(item for item in pending if item["task_id"] == "T00017")
    client.post(f"/proposals/{blocked['proposal_id']}/approve")
    rows = store.list_changes()
    assert any(row["field_name"] == "status" and row["new_value"] == "Blocked" and row["proposal_id"] == blocked["proposal_id"] for row in rows)
    again = store.record_change(
        {
            "change_id": "C-dup",
            "proposal_id": blocked["proposal_id"],
            "project_id": "PRJ001",
            "subject": "T00017",
            "task_id": "T00017",
            "field_name": "status",
            "old_value": "In progress",
            "new_value": "Blocked",
            "source_kind": "meeting",
            "source_id": "M0003",
        }
    )
    assert again is False
    assert sum(1 for row in store.list_changes() if row["field_name"] == "status" and row["task_id"] == "T00017") == 1


def test_off_schema_output_is_repaired_once_then_queued(desk, store):
    calls = {"n": 0}

    def parse():
        calls["n"] += 1
        raise ValueError("not schema")

    def repair():
        calls["n"] += 1
        raise ValueError("still not schema")

    value, status, _error = run_with_repair(parse, repair)
    assert value is None
    assert status == "review_queue"
    assert calls["n"] == 2

    proposal_id = desk.queue_off_schema("PRJ001", "meeting", "M0001", "still not schema", "B-test")
    row = store.get_proposal(proposal_id)
    assert row["off_schema"] == 1
    assert row["reconciliation"] == "conflict_or_ambiguous"
    assert row["applicable"] is False


def test_notify_skips_without_a_url():
    assert notify_draft_ready("", {"event": "draft_ready"}) == "skipped"


def test_postgres_schema_locks_task_writes_to_the_service_role():
    sql = Path("sql/schema.sql").read_text()
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "tasks_write_service" in sql
    assert "approvals_insert_reviewer" in sql
    assert "vector(64)" in sql
    assert "service_role" in sql
