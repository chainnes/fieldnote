from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from fieldnote.embed import chunk_text, cosine, embed
from fieldnote.match import Scored, normalize, score_title

SCHEMA = """
CREATE TABLE projects (
  project_id TEXT PRIMARY KEY,
  project_name TEXT,
  region TEXT,
  start_date TEXT,
  target_end_date TEXT,
  contract_type TEXT
);
CREATE TABLE people (
  person_id TEXT PRIMARY KEY,
  display_name TEXT,
  role TEXT,
  discipline TEXT,
  region TEXT,
  email TEXT
);
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  project_id TEXT,
  task_title TEXT,
  discipline TEXT,
  owner_id TEXT,
  owner_name TEXT,
  planned_start TEXT,
  planned_due TEXT,
  status TEXT,
  percent_complete INTEGER,
  priority TEXT,
  notes TEXT,
  origin TEXT
);
CREATE TABLE dependencies (
  dependency_id TEXT PRIMARY KEY,
  predecessor_task_id TEXT,
  successor_task_id TEXT,
  dependency_type TEXT
);
CREATE TABLE snapshots (
  task_id TEXT,
  project_id TEXT,
  task_title TEXT,
  owner_id TEXT,
  owner_name TEXT,
  planned_start TEXT,
  planned_due TEXT,
  status TEXT,
  percent_complete INTEGER,
  priority TEXT,
  notes TEXT,
  snapshot TEXT,
  PRIMARY KEY (task_id, snapshot)
);
CREATE TABLE meetings (
  meeting_id TEXT PRIMARY KEY,
  project_id TEXT,
  meeting_datetime TEXT,
  title TEXT,
  attendees TEXT,
  notes_text TEXT
);
CREATE TABLE emails (
  email_id TEXT PRIMARY KEY,
  project_id TEXT,
  sent_datetime TEXT,
  sender TEXT,
  recipients TEXT,
  subject TEXT,
  body TEXT
);
CREATE TABLE batches (
  batch_id TEXT PRIMARY KEY,
  project_id TEXT,
  source_kind TEXT,
  source_id TEXT,
  provider TEXT,
  model TEXT,
  tool_trace_json TEXT,
  notes TEXT,
  created_at TEXT
);
CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY,
  batch_id TEXT,
  project_id TEXT,
  source_kind TEXT,
  source_id TEXT,
  kind TEXT,
  task_id TEXT,
  title TEXT,
  owner_id TEXT,
  owner_name TEXT,
  due_date TEXT,
  from_date TEXT,
  status TEXT,
  evidence_quote TEXT,
  rationale TEXT,
  confidence REAL,
  attention TEXT,
  conflict_reason TEXT,
  cousins_json TEXT,
  applicable INTEGER,
  review_status TEXT,
  created_at TEXT,
  resolved_at TEXT,
  resolver_note TEXT,
  reconciliation TEXT,
  reconcile_reason TEXT,
  vector_score REAL,
  urgency_score INTEGER,
  urgency_reason TEXT,
  off_schema INTEGER DEFAULT 0,
  support_source TEXT
);
CREATE TABLE source_documents (
  document_id TEXT PRIMARY KEY,
  project_id TEXT,
  source_kind TEXT,
  source_id TEXT,
  title TEXT,
  body TEXT,
  happened_at TEXT
);
CREATE TABLE source_chunks (
  chunk_id TEXT PRIMARY KEY,
  document_id TEXT,
  project_id TEXT,
  source_kind TEXT,
  source_id TEXT,
  position INTEGER,
  text TEXT,
  embedding TEXT
);
CREATE TABLE project_members (
  project_id TEXT,
  person_id TEXT,
  role TEXT,
  PRIMARY KEY (project_id, person_id)
);
CREATE TABLE change_log (
  change_id TEXT PRIMARY KEY,
  dedupe_hash TEXT UNIQUE,
  proposal_id TEXT,
  project_id TEXT,
  task_id TEXT,
  field_name TEXT,
  old_value TEXT,
  new_value TEXT,
  source_kind TEXT,
  source_id TEXT,
  applied_at TEXT
);
CREATE TABLE audit (
  audit_id TEXT PRIMARY KEY,
  created_at TEXT,
  action TEXT,
  proposal_id TEXT,
  project_id TEXT,
  source_kind TEXT,
  source_id TEXT,
  task_id TEXT,
  summary TEXT,
  evidence_quote TEXT,
  before_json TEXT,
  after_json TEXT
);
"""


def dedupe_hash(project_id: str, subject: str, field: str, new_value: str) -> str:
    import hashlib

    raw = "|".join([project_id, subject, field, new_value or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pretty_date(value: str | date | None) -> str:
    if not value:
        return "—"
    if isinstance(value, date):
        return f"{value.day} {value.strftime('%b %Y')}"
    try:
        day = datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return str(value)
    return f"{day.day} {day.strftime('%b %Y')}"


class Store:
    def __init__(self, path: Path, seed_dir: Path, as_of: date):
        self.path = Path(path)
        self.seed_dir = Path(seed_dir)
        self.as_of = as_of
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if fresh:
            self._create()
            self._load_seed()
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def reset(self) -> None:
        self.conn.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = Path(str(self.path) + suffix) if suffix else self.path
            if target.exists():
                target.unlink()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create()
        self._load_seed()

    def _create(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _load_seed(self) -> None:
        seed = self.seed_dir
        required = [
            "projects.csv",
            "people.csv",
            "tasks_master.csv",
            "dependencies.csv",
            "plan_snapshots.csv",
            "meeting_notes.jsonl",
            "emails.csv",
        ]
        missing = [name for name in required if not (seed / name).exists()]
        if missing:
            raise FileNotFoundError(f"Seed data missing in {seed}: {', '.join(missing)}")

        with (seed / "projects.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.conn.executemany(
            "INSERT INTO projects VALUES (?,?,?,?,?,?)",
            [
                (
                    row["project_id"],
                    row["project_name"],
                    row["region"],
                    row["start_date"],
                    row["target_end_date"],
                    row["contract_type"],
                )
                for row in rows
            ],
        )
        with (seed / "people.csv").open(newline="", encoding="utf-8") as handle:
            people = list(csv.DictReader(handle))
        self.conn.executemany(
            "INSERT INTO people VALUES (?,?,?,?,?,?)",
            [
                (
                    row["person_id"],
                    row["display_name"],
                    row["role"],
                    row["discipline"],
                    row["region"],
                    row["email"],
                )
                for row in people
            ],
        )
        with (seed / "tasks_master.csv").open(newline="", encoding="utf-8") as handle:
            tasks = list(csv.DictReader(handle))
        self.conn.executemany(
            """INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'seed')""",
            [
                (
                    row["task_id"],
                    row["project_id"],
                    row["task_title"],
                    row["discipline"],
                    row["owner_id"],
                    row["owner_name"],
                    row["planned_start"],
                    row["planned_due"],
                    row["status"],
                    int(row["percent_complete"] or 0),
                    row["priority"],
                    row["notes"],
                )
                for row in tasks
            ],
        )
        with (seed / "dependencies.csv").open(newline="", encoding="utf-8") as handle:
            deps = list(csv.DictReader(handle))
        self.conn.executemany(
            "INSERT INTO dependencies VALUES (?,?,?,?)",
            [
                (
                    row["dependency_id"],
                    row["predecessor_task_id"],
                    row["successor_task_id"],
                    row["dependency_type"],
                )
                for row in deps
            ],
        )
        with (seed / "plan_snapshots.csv").open(newline="", encoding="utf-8") as handle:
            snaps = list(csv.DictReader(handle))
        self.conn.executemany(
            "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["task_id"],
                    row["project_id"],
                    row["task_title"],
                    row["owner_id"],
                    row["owner_name"],
                    row["planned_start"],
                    row["planned_due"],
                    row["status"],
                    int(row["percent_complete"] or 0),
                    row["priority"],
                    row["notes"],
                    row["snapshot"],
                )
                for row in snaps
            ],
        )
        with (seed / "meeting_notes.jsonl").open(encoding="utf-8") as handle:
            meetings = [json.loads(line) for line in handle if line.strip()]
        self.conn.executemany(
            "INSERT INTO meetings VALUES (?,?,?,?,?,?)",
            [
                (
                    row["meeting_id"],
                    row["project_id"],
                    row["meeting_datetime"],
                    row["title"],
                    row["attendees"],
                    row["notes_text"],
                )
                for row in meetings
            ],
        )
        with (seed / "emails.csv").open(newline="", encoding="utf-8") as handle:
            emails = list(csv.DictReader(handle))
        self.conn.executemany(
            "INSERT INTO emails (email_id, project_id, sent_datetime, sender, recipients, subject, body) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    row["email_id"],
                    row["project_id"],
                    row["sent_datetime"],
                    row["from"],
                    row["to"],
                    row["subject"],
                    row["body"],
                )
                for row in emails
            ],
        )
        self.conn.commit()
        self._index_sources()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_documents (
              document_id TEXT PRIMARY KEY,
              project_id TEXT,
              source_kind TEXT,
              source_id TEXT,
              title TEXT,
              body TEXT,
              happened_at TEXT
            );
            CREATE TABLE IF NOT EXISTS source_chunks (
              chunk_id TEXT PRIMARY KEY,
              document_id TEXT,
              project_id TEXT,
              source_kind TEXT,
              source_id TEXT,
              position INTEGER,
              text TEXT,
              embedding TEXT
            );
            CREATE TABLE IF NOT EXISTS project_members (
              project_id TEXT,
              person_id TEXT,
              role TEXT,
              PRIMARY KEY (project_id, person_id)
            );
            CREATE TABLE IF NOT EXISTS change_log (
              change_id TEXT PRIMARY KEY,
              dedupe_hash TEXT UNIQUE,
              proposal_id TEXT,
              project_id TEXT,
              task_id TEXT,
              field_name TEXT,
              old_value TEXT,
              new_value TEXT,
              source_kind TEXT,
              source_id TEXT,
              applied_at TEXT
            );
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(proposals)")}
        additions = {
            "reconciliation": "TEXT",
            "reconcile_reason": "TEXT",
            "vector_score": "REAL",
            "urgency_score": "INTEGER",
            "urgency_reason": "TEXT",
            "off_schema": "INTEGER DEFAULT 0",
            "support_source": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE proposals ADD COLUMN {name} {ddl}")
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
        meetings = self.conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        if meetings and count == 0:
            self._index_sources()

    def _index_sources(self) -> None:
        """Normalize notes and emails into source_documents and embedded chunks."""
        self.conn.execute("DELETE FROM source_chunks")
        self.conn.execute("DELETE FROM source_documents")
        self.conn.execute("DELETE FROM project_members")
        documents = []
        for row in self._all("SELECT * FROM meetings"):
            documents.append(
                (
                    f"doc-{row['meeting_id']}",
                    row["project_id"],
                    "meeting",
                    row["meeting_id"],
                    row["title"],
                    row["notes_text"],
                    row["meeting_datetime"],
                )
            )
        for row in self._all("SELECT * FROM emails"):
            documents.append(
                (
                    f"doc-{row['email_id']}",
                    row["project_id"],
                    "email",
                    row["email_id"],
                    row["subject"],
                    row["body"],
                    row["sent_datetime"],
                )
            )
        self.conn.executemany(
            "INSERT INTO source_documents VALUES (?,?,?,?,?,?,?)",
            documents,
        )
        chunks = []
        for document_id, project_id, kind, source_id, _title, body, _when in documents:
            for position, text in enumerate(chunk_text(body)):
                chunks.append(
                    (
                        f"{document_id}-{position}",
                        document_id,
                        project_id,
                        kind,
                        source_id,
                        position,
                        text,
                        json.dumps(embed(text)),
                    )
                )
        self.conn.executemany(
            "INSERT INTO source_chunks VALUES (?,?,?,?,?,?,?,?)",
            chunks,
        )
        members = []
        people = [row["person_id"] for row in self._all("SELECT person_id FROM people")]
        for project in self._all("SELECT project_id FROM projects"):
            for person_id in people:
                members.append((project["project_id"], person_id, "reviewer"))
        self.conn.executemany(
            "INSERT INTO project_members VALUES (?,?,?)",
            members,
        )
        self.conn.commit()

    def search_sources(self, project_id: str, query: str, limit: int = 5) -> list[dict]:
        query_vector = embed(query)
        hits = []
        rows = self._all(
            """
            SELECT c.*, d.title AS document_title
            FROM source_chunks c
            JOIN source_documents d ON d.document_id = c.document_id
            WHERE c.project_id = ?
            """,
            (project_id,),
        )
        for row in rows:
            score = cosine(query_vector, json.loads(row["embedding"]))
            if score <= 0:
                continue
            hits.append(
                {
                    "chunk_id": row["chunk_id"],
                    "source_kind": row["source_kind"],
                    "source_id": row["source_id"],
                    "title": row["document_title"],
                    "text": row["text"],
                    "score": score,
                }
            )
        hits.sort(key=lambda item: (-item["score"], item["source_id"], item["chunk_id"]))
        return hits[:limit]

    def similar_tasks(self, project_id: str, query: str, limit: int = 5) -> list[dict]:
        query_vector = embed(query)
        ranked = []
        for task in self._all("SELECT * FROM tasks WHERE project_id = ?", (project_id,)):
            score = cosine(query_vector, embed(task["task_title"]))
            ranked.append(
                {
                    "task_id": task["task_id"],
                    "title": task["task_title"],
                    "score": score,
                    "owner_name": task["owner_name"],
                    "planned_due": task["planned_due"],
                    "status": task["status"],
                }
            )
        ranked.sort(key=lambda item: (-item["score"], item["task_id"]))
        return ranked[:limit]

    def successor_count(self, task_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM dependencies WHERE predecessor_task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row[0] if row else 0)

    def record_change(self, row: dict) -> bool:
        """Insert one field change. False when dedupe_hash was already applied."""
        digest = dedupe_hash(
            row.get("project_id") or "",
            row.get("subject") or row.get("task_id") or "",
            row.get("field_name") or "",
            row.get("new_value") or "",
        )
        try:
            self.conn.execute(
                """INSERT INTO change_log (
                    change_id, dedupe_hash, proposal_id, project_id, task_id, field_name,
                    old_value, new_value, source_kind, source_id, applied_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["change_id"],
                    digest,
                    row.get("proposal_id"),
                    row.get("project_id"),
                    row.get("task_id"),
                    row.get("field_name"),
                    row.get("old_value") or "",
                    row.get("new_value") or "",
                    row.get("source_kind"),
                    row.get("source_id"),
                    row.get("applied_at") or utc_now(),
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False

    def list_changes(self, limit: int = 40) -> list[dict]:
        return self._all(
            "SELECT * FROM change_log ORDER BY applied_at DESC, change_id DESC LIMIT ?",
            (limit,),
        )

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict:
        tables = ["projects", "people", "tasks", "dependencies", "snapshots", "meetings", "emails"]
        return {name: self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}

    def list_projects(self) -> list[dict]:
        projects = self._all("SELECT * FROM projects ORDER BY project_id")
        for project in projects:
            project.update(self.project_health(project["project_id"]))
        return projects

    def get_project(self, project_id: str) -> dict | None:
        project = self._one("SELECT * FROM projects WHERE project_id = ?", (project_id,))
        if not project:
            return None
        project.update(self.project_health(project_id))
        return project

    def list_people(self) -> list[dict]:
        return self._all("SELECT * FROM people ORDER BY display_name")

    def get_person(self, person_id: str) -> dict | None:
        return self._one("SELECT * FROM people WHERE person_id = ?", (person_id,))

    def resolve_person(self, name: str) -> list[dict]:
        target = normalize(name)
        if not target:
            return []
        people = self.list_people()
        exact = [person for person in people if normalize(person["display_name"]) == target]
        if exact:
            return exact
        # A single given name is too ambiguous in this directory (two Aishas, several Linas).
        if " " not in target:
            return []
        partial = [
            person
            for person in people
            if target in normalize(person["display_name"]) or normalize(person["display_name"]) in target
        ]
        return partial

    def list_tasks(self, project_id: str) -> list[dict]:
        tasks = self._all(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY planned_due, task_id",
            (project_id,),
        )
        snaps = {
            row["task_id"]: row
            for row in self._all(
                "SELECT * FROM snapshots WHERE project_id = ? AND snapshot = 'current'",
                (project_id,),
            )
        }
        blocked = {task["task_id"] for task in self._all("SELECT task_id, status FROM tasks") if task["status"] == "Blocked"}
        upstream = {}
        for dep in self._all("SELECT * FROM dependencies"):
            if dep["predecessor_task_id"] in blocked:
                upstream.setdefault(dep["successor_task_id"], []).append(dep["predecessor_task_id"])
        clock = self.as_of.isoformat()
        for task in tasks:
            snap = snaps.get(task["task_id"])
            drift = []
            if snap:
                if snap["planned_due"] != task["planned_due"]:
                    drift.append(f"snapshot due {pretty_date(snap['planned_due'])}")
                if snap["status"] != task["status"]:
                    drift.append(f"snapshot {snap['status']}")
                if snap["owner_name"] != task["owner_name"]:
                    drift.append(f"snapshot owner {snap['owner_name']}")
            task["drift"] = drift
            task["overdue"] = task["status"] != "Done" and bool(task["planned_due"]) and task["planned_due"] < clock
            task["upstream_blocked"] = upstream.get(task["task_id"], [])
        return tasks

    def get_task(self, task_id: str) -> dict | None:
        return self._one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))

    def search_tasks(self, project_id: str, query: str, limit: int = 8) -> list[Scored]:
        ranked: list[Scored] = []
        for task in self._all("SELECT * FROM tasks WHERE project_id = ?", (project_id,)):
            ranked.append(
                Scored(
                    task_id=task["task_id"],
                    title=task["task_title"],
                    score=round(score_title(query, task["task_title"]), 4),
                    owner_id=task["owner_id"],
                    owner_name=task["owner_name"],
                    planned_due=task["planned_due"],
                    status=task["status"],
                    priority=task["priority"],
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.task_id))
        return ranked[:limit]

    def dependencies_for(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        preds = self._all(
            """
            SELECT d.dependency_type, t.task_id, t.task_title, t.status, t.planned_due, t.project_id
            FROM dependencies d JOIN tasks t ON t.task_id = d.predecessor_task_id
            WHERE d.successor_task_id = ?
            ORDER BY t.task_id
            """,
            (task_id,),
        )
        succs = self._all(
            """
            SELECT d.dependency_type, t.task_id, t.task_title, t.status, t.planned_due, t.project_id
            FROM dependencies d JOIN tasks t ON t.task_id = d.successor_task_id
            WHERE d.predecessor_task_id = ?
            ORDER BY t.task_id
            """,
            (task_id,),
        )
        return {
            "task_id": task_id,
            "title": task["task_title"] if task else "",
            "predecessors": preds,
            "successors": succs,
        }

    def project_health(self, project_id: str) -> dict:
        tasks = self._all("SELECT * FROM tasks WHERE project_id = ?", (project_id,))
        clock = self.as_of.isoformat()
        snaps = {
            row["task_id"]: row
            for row in self._all(
                "SELECT task_id, planned_due, status, owner_name FROM snapshots WHERE project_id = ? AND snapshot = 'current'",
                (project_id,),
            )
        }
        drift = 0
        for task in tasks:
            snap = snaps.get(task["task_id"])
            if snap and (
                snap["planned_due"] != task["planned_due"]
                or snap["status"] != task["status"]
                or snap["owner_name"] != task["owner_name"]
            ):
                drift += 1
        blocked_ids = {row["task_id"] for row in self._all("SELECT task_id FROM tasks WHERE status = 'Blocked'")}
        task_ids = {task["task_id"] for task in tasks}
        downstream = set()
        for dep in self._all("SELECT predecessor_task_id, successor_task_id FROM dependencies"):
            if dep["predecessor_task_id"] in blocked_ids and dep["successor_task_id"] in task_ids:
                downstream.add(dep["successor_task_id"])
        open_tasks = [task for task in tasks if task["status"] != "Done"]
        return {
            "task_count": len(tasks),
            "open_count": len(open_tasks),
            "blocked_count": sum(1 for task in tasks if task["status"] == "Blocked"),
            "overdue_count": sum(
                1 for task in tasks if task["status"] != "Done" and task["planned_due"] and task["planned_due"] < clock
            ),
            "drift_count": drift,
            "downstream_blocked": len(downstream - {task["task_id"] for task in tasks if task["status"] == "Done"}),
        }

    def project_risks(self, project_id: str) -> dict:
        health = self.project_health(project_id)
        tasks = self.list_tasks(project_id)
        blocked = [task for task in tasks if task["status"] == "Blocked"][:5]
        overdue = [task for task in tasks if task["overdue"]][:5]
        return {
            "project_id": project_id,
            "as_of": self.as_of.isoformat(),
            **health,
            "blocked_samples": [
                {"task_id": task["task_id"], "title": task["task_title"], "due": task["planned_due"]}
                for task in blocked
            ],
            "overdue_samples": [
                {"task_id": task["task_id"], "title": task["task_title"], "due": task["planned_due"]}
                for task in overdue
            ],
        }

    def get_source(self, kind: str, source_id: str) -> dict | None:
        if kind == "meeting":
            row = self._one("SELECT * FROM meetings WHERE meeting_id = ?", (source_id,))
            if not row:
                return None
            return {
                "kind": "meeting",
                "source_id": row["meeting_id"],
                "project_id": row["project_id"],
                "when": row["meeting_datetime"],
                "label": row["title"],
                "meta": row["attendees"],
                "text": row["notes_text"],
            }
        if kind == "email":
            row = self._one("SELECT * FROM emails WHERE email_id = ?", (source_id,))
            if not row:
                return None
            return {
                "kind": "email",
                "source_id": row["email_id"],
                "project_id": row["project_id"],
                "when": row["sent_datetime"],
                "label": row["subject"],
                "meta": f"From {row['sender']} to {row['recipients']}",
                "sender": row["sender"],
                "recipients": row["recipients"],
                "text": row["body"],
            }
        return None

    def list_sources(self, project_id: str) -> dict:
        meetings = self._all(
            "SELECT * FROM meetings WHERE project_id = ? ORDER BY meeting_datetime DESC, meeting_id",
            (project_id,),
        )
        emails = self._all(
            "SELECT * FROM emails WHERE project_id = ? ORDER BY sent_datetime DESC, email_id",
            (project_id,),
        )
        return {"meetings": meetings, "emails": emails}

    def next_id(self, table: str, column: str, prefix: str) -> str:
        rows = self.conn.execute(f"SELECT {column} FROM {table} WHERE {column} LIKE ?", (prefix + "%",)).fetchall()
        numbers = []
        for row in rows:
            tail = row[0][len(prefix) :]
            if tail.isdigit():
                numbers.append(int(tail))
        return f"{prefix}{(max(numbers) if numbers else 0) + 1:04d}"

    def count_pending(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM proposals WHERE review_status = 'pending'").fetchone()[0]

    def insert_batch(self, row: dict) -> None:
        self.conn.execute(
            """INSERT INTO batches VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                row["batch_id"],
                row["project_id"],
                row["source_kind"],
                row["source_id"],
                row["provider"],
                row["model"],
                row["tool_trace_json"],
                row["notes"],
                row["created_at"],
            ),
        )
        self.conn.commit()

    def insert_proposal(self, row: dict) -> None:
        self.conn.execute(
            """INSERT INTO proposals (
                proposal_id, batch_id, project_id, source_kind, source_id, kind, task_id, title,
                owner_id, owner_name, due_date, from_date, status, evidence_quote, rationale,
                confidence, attention, conflict_reason, cousins_json, applicable, review_status,
                created_at, resolved_at, resolver_note, reconciliation, reconcile_reason,
                vector_score, urgency_score, urgency_reason, off_schema, support_source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["proposal_id"],
                row["batch_id"],
                row["project_id"],
                row["source_kind"],
                row["source_id"],
                row["kind"],
                row.get("task_id"),
                row["title"],
                row.get("owner_id"),
                row.get("owner_name"),
                row.get("due_date"),
                row.get("from_date"),
                row.get("status"),
                row["evidence_quote"],
                row.get("rationale") or "",
                row.get("confidence") or 0,
                row.get("attention") or "review",
                row.get("conflict_reason"),
                json.dumps(row.get("cousins") or []),
                1 if row.get("applicable") else 0,
                "pending",
                row["created_at"],
                None,
                None,
                row.get("reconciliation") or "",
                row.get("reconcile_reason") or "",
                row.get("vector_score") or 0,
                int(row.get("urgency_score") or 0),
                row.get("urgency_reason") or "",
                1 if row.get("off_schema") else 0,
                row.get("support_source") or "",
            ),
        )
        self.conn.commit()

    def supersede_pending(self, project_id: str, kind: str, source_id: str) -> int:
        cur = self.conn.execute(
            """UPDATE proposals SET review_status = 'superseded', resolved_at = ?, resolver_note = ?
               WHERE project_id = ? AND source_kind = ? AND source_id = ? AND review_status = 'pending'""",
            (utc_now(), "Set aside because the note was read again.", project_id, kind, source_id),
        )
        self.conn.commit()
        return cur.rowcount

    def get_proposal(self, proposal_id: str) -> dict | None:
        row = self._one("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,))
        if not row:
            return None
        row["cousins"] = json.loads(row.pop("cousins_json") or "[]")
        row["applicable"] = bool(row["applicable"])
        return row

    def list_proposals(self, *, project_id: str | None = None, source_id: str | None = None, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM proposals WHERE 1=1"
        args: list = []
        if project_id:
            sql += " AND project_id = ?"
            args.append(project_id)
        if source_id:
            sql += " AND source_id = ?"
            args.append(source_id)
        if status:
            sql += " AND review_status = ?"
            args.append(status)
        sql += " ORDER BY created_at, proposal_id"
        rows = self._all(sql, tuple(args))
        for row in rows:
            row["cousins"] = json.loads(row.pop("cousins_json") or "[]")
            row["applicable"] = bool(row["applicable"])
        return rows

    def latest_batch(self, project_id: str, kind: str, source_id: str) -> dict | None:
        return self._one(
            """SELECT * FROM batches WHERE project_id = ? AND source_kind = ? AND source_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (project_id, kind, source_id),
        )

    def mark_proposal(self, proposal_id: str, status: str, note: str) -> None:
        self.conn.execute(
            "UPDATE proposals SET review_status = ?, resolved_at = ?, resolver_note = ? WHERE proposal_id = ?",
            (status, utc_now(), note, proposal_id),
        )
        self.conn.commit()

    def update_task(self, task_id: str, fields: dict) -> None:
        allowed = {"task_title", "owner_id", "owner_name", "planned_due", "status", "percent_complete", "notes", "origin"}
        sets = []
        args = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            args.append(value)
        if not sets:
            return
        args.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?", args)
        self.conn.commit()

    def insert_task(self, row: dict) -> None:
        self.conn.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["task_id"],
                row["project_id"],
                row["task_title"],
                row.get("discipline") or "General",
                row.get("owner_id") or "",
                row.get("owner_name") or "",
                row.get("planned_start") or "",
                row.get("planned_due") or "",
                row.get("status") or "Not started",
                int(row.get("percent_complete") or 0),
                row.get("priority") or "Medium",
                row.get("notes") or "",
                row.get("origin") or "approved",
            ),
        )
        self.conn.commit()

    def add_audit(self, row: dict) -> None:
        self.conn.execute(
            "INSERT INTO audit VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["audit_id"],
                row["created_at"],
                row["action"],
                row.get("proposal_id"),
                row.get("project_id"),
                row.get("source_kind"),
                row.get("source_id"),
                row.get("task_id"),
                row.get("summary") or "",
                row.get("evidence_quote") or "",
                json.dumps(row.get("before"), default=str) if row.get("before") is not None else None,
                json.dumps(row.get("after"), default=str) if row.get("after") is not None else None,
            ),
        )
        self.conn.commit()

    def list_audit(self) -> list[dict]:
        return self._all("SELECT * FROM audit ORDER BY created_at DESC, audit_id DESC")
