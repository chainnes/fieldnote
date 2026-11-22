def test_seed_loads_the_public_pack(store):
    counts = store.counts()
    assert counts == {
        "projects": 4,
        "people": 18,
        "tasks": 280,
        "dependencies": 50,
        "snapshots": 560,
        "meetings": 72,
        "emails": 160,
    }
    assert store.count_pending() == 0


def test_home_and_health(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "Harbour Bridge Expansion" in home.text
    assert "Charlene Cordero" in home.text
    health = client.get("/health")
    assert health.json()["ok"] is True
    assert health.json()["provider"] == "mock"
    assert health.json()["tasks"] == 280


def test_draft_approve_reject_and_reset_round_trip(client, store):
    drafted = client.post("/p/PRJ001/s/meeting/M0003/draft", follow_redirects=True)
    assert drafted.status_code == 200
    assert "Mark CDE folder structure as blocked pending missing survey" in drafted.text
    assert "search_tasks" in drafted.text
    assert store.get_task("T00017")["status"] == "In progress"

    pending = store.list_proposals(source_id="M0003", status="pending")
    blocked = next(item for item in pending if item["task_id"] == "T00017")
    weekly = next(item for item in pending if item["kind"] == "conflict" and "weekly" in item["title"])

    approved = client.post(f"/proposals/{blocked['proposal_id']}/approve", follow_redirects=True)
    assert approved.status_code == 200
    assert store.get_task("T00017")["status"] == "Blocked"
    audit = store.list_audit()
    assert audit[0]["action"] == "approved"
    assert audit[0]["evidence_quote"].startswith("Mark CDE folder structure")

    rejected = client.post(
        f"/proposals/{weekly['proposal_id']}/reject",
        data={"reason": "Five weekly reports. Not guessing."},
        follow_redirects=True,
    )
    assert rejected.status_code == 200
    assert store.get_proposal(weekly["proposal_id"])["review_status"] == "rejected"
    assert "Five weekly reports" in store.list_audit()[0]["summary"]

    page = client.get("/p/PRJ001")
    assert 'data-task="T00017"' in page.text
    assert 'data-status="Blocked"' in page.text

    client.post("/reset", follow_redirects=True)
    assert store.get_task("T00017")["status"] == "In progress"
    assert store.count_pending() == 0
    assert store.list_audit() == []


def test_live_provider_without_a_key_does_not_call_out(client):
    client.app.state.desk.settings = client.app.state.desk.settings.__class__(
        **{**client.app.state.desk.settings.__dict__, "provider": "openai", "api_key": ""}
    )
    response = client.post("/p/PRJ001/s/meeting/M0003/draft", follow_redirects=False)
    assert response.status_code == 303
    assert "LLM_API_KEY" in response.headers["location"]


def test_missing_project(client):
    assert client.get("/p/PRJ999").status_code == 404
