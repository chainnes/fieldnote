from fieldnote.tools import CURRENT_STORE, search_tasks


def test_search_tool_returns_the_cde_row(store):
    token = CURRENT_STORE.set(store)
    try:
        import json

        payload = json.loads(search_tasks.invoke({"project_id": "PRJ001", "query": "CDE folder structure"}))
    finally:
        CURRENT_STORE.reset(token)
    assert payload["matches"][0]["task_id"] == "T00017"
    assert payload["matches"][0]["score"] == 1


def test_mock_graph_calls_tools_and_does_not_write_the_plan(desk, store):
    before = store.get_task("T00017")["status"]
    desk.draft("PRJ001", "meeting", "M0003")
    after = store.get_task("T00017")["status"]
    assert after == before == "In progress"

    batch = store.latest_batch("PRJ001", "meeting", "M0003")
    import json

    trace = json.loads(batch["tool_trace_json"])
    tools = [call["tool"] for call in trace]
    assert "search_tasks" in tools
    assert "resolve_person" in tools
    assert "project_risks" in tools
    assert "task_dependencies" in tools
    queries = [call["args"].get("query") for call in trace if call["tool"] == "search_tasks"]
    assert "CDE folder structure" in queries

    proposals = store.list_proposals(source_id="M0003", status="pending")
    by_quote = {item["evidence_quote"]: item for item in proposals}
    blocked = by_quote["Mark CDE folder structure as blocked pending missing survey"]
    assert blocked["task_id"] == "T00017"
    assert blocked["kind"] == "status_update"
    assert blocked["status"] == "Blocked"
    assert blocked["attention"] == "routine"
    assert blocked["applicable"] is True

    created = next(item for item in proposals if item["evidence_quote"].startswith("Create new task: risk register"))
    assert created["kind"] == "new_task"
    assert created["task_id"] is None
    assert created["applicable"] is True

    shifted = next(item for item in proposals if item["evidence_quote"].startswith("Shift risk register"))
    assert shifted["kind"] == "conflict"
    assert shifted["applicable"] is False

    weekly = next(item for item in proposals if "weekly update" in item["evidence_quote"])
    assert weekly["kind"] == "conflict"
    assert weekly["task_id"] is None

    for item in proposals:
        assert item["evidence_quote"] in store.get_source("meeting", "M0003")["text"]


def test_email_links_client_presentation_without_inventing_a_person(desk, store):
    desk.draft("PRJ001", "email", "E00002")
    proposals = store.list_proposals(source_id="E00002", status="pending")
    linked = next(item for item in proposals if "client presentation" in item["evidence_quote"])
    assert linked["task_id"] == "T00044"
    assert linked["owner_name"] == "Sofia Singh"
    assert linked["owner_id"] == "P011"
    assert linked["attention"] == "review"
    assert linked["kind"] == "owner_change"
