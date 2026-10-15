"""Ownership-aware SQLite store.

Every user-facing resource lookup includes the owner id in the query.  The
store intentionally does not expose a generic "get by id" method for owned
resources, making the safe path the easiest path for route handlers.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


class ResourceNotFound(LookupError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class AppStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        schema = """
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,
            hidden INTEGER NOT NULL DEFAULT 0,
            snapshot_json TEXT,
            plan_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_owner_updated
            ON conversations(owner_id, hidden, updated_at DESC);
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            owner_id TEXT NOT NULL REFERENCES users(id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(owner_id, conversation_id, created_at);
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(id),
            task_id TEXT,
            kind TEXT NOT NULL CHECK (kind IN ('upload', 'dataset', 'output')),
            relative_path TEXT NOT NULL,
            original_name TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            deleted_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_files_owner_kind ON files(owner_id, kind, deleted_at);
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(id),
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_executor TEXT NOT NULL,
            resolved_executor TEXT NOT NULL,
            recommendation_reason TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            retry_of TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            ray_job_id TEXT,
            result_relative_path TEXT,
            log_text TEXT NOT NULL DEFAULT '',
            error_text TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_owner_created ON tasks(owner_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(id),
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            task_id TEXT,
            prompt TEXT NOT NULL,
            response TEXT,
            status TEXT NOT NULL,
            model_name TEXT,
            duration_ms INTEGER,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            error_text TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner_id, started_at DESC);
        CREATE TABLE IF NOT EXISTS invocations (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id),
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            request_json TEXT,
            response_json TEXT,
            duration_ms INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_invocations_run ON invocations(run_id, created_at);
        CREATE TABLE IF NOT EXISTS run_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_events_run_sequence
            ON run_events(run_id, sequence);
        CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL REFERENCES users(id),
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
        CREATE TABLE IF NOT EXISTS scheduler_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scheduler_lease (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            owner TEXT,
            expires_at TEXT
        );
        INSERT OR IGNORE INTO scheduler_lease(singleton, owner, expires_at)
            VALUES(1, NULL, NULL);
        """
        with self.connect() as connection:
            connection.executescript(schema)
            self._migrate_task_columns(connection)
            connection.execute("PRAGMA optimize")

    @staticmethod
    def _migrate_task_columns(connection: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        columns = {
            "resource_class": "TEXT NOT NULL DEFAULT 'cpu'",
            "requested_cpus": "REAL NOT NULL DEFAULT 1",
            "requested_gpus": "REAL NOT NULL DEFAULT 0",
            "assigned_gpu_ids": "TEXT",
            "queue_reason": "TEXT",
            "queue_sequence": "INTEGER",
            "dispatch_attempt": "INTEGER NOT NULL DEFAULT 0",
            "scheduler_owner": "TEXT",
            "lease_expires_at": "TEXT",
            "heartbeat_at": "TEXT",
            "external_id": "TEXT",
            "runtime_metadata": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(status, queue_sequence, created_at)"
        )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_user(
        self, username: str, display_name: str, password_hash: str, role: str = "user"
    ) -> dict[str, Any]:
        user_id = _id("usr")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, role, created_at) VALUES(?,?,?,?,?,?)",
                (user_id, username.strip(), display_name.strip(), password_hash, role, _now()),
            )
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row(row) or {}

    def find_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
            ).fetchone()
        return self._row(row)

    def require_user(self, user_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise ResourceNotFound("User not found")
        return self._row(row) or {}

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, username, display_name, role, status, must_change_password, created_at, last_login_at "
                "FROM users ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_user_admin(
        self, user_id: str, *, status: str | None = None, role: str | None = None,
        password_hash: str | None = None, must_change_password: bool | None = None
    ) -> None:
        updates, values = [], []
        for column, value in (("status", status), ("role", role), ("password_hash", password_hash)):
            if value is not None:
                updates.append(f"{column} = ?")
                values.append(value)
        if must_change_password is not None:
            updates.append("must_change_password = ?")
            values.append(int(must_change_password))
        if not updates:
            return
        values.append(user_id)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id = ?", values
            )
            if cursor.rowcount != 1:
                raise ResourceNotFound("User not found")

    def count_active_admins(self) -> int:
        with self.connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'active'"
            ).fetchone()[0])

    def create_auth_session(self, user_id: str, ttl_hours: int = 24 * 7) -> str:
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        token_hash = sha256(token.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO auth_sessions(token_hash, user_id, expires_at, created_at) VALUES(?,?,?,?)",
                (token_hash, user_id, expires, _now()),
            )
            connection.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), user_id))
        return token

    def user_for_auth_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = sha256(token.encode()).hexdigest()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT u.* FROM auth_sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ? AND u.status = 'active'",
                (token_hash, _now()),
            ).fetchone()
        return self._row(row)

    def delete_auth_session(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (sha256(token.encode()).hexdigest(),),
            )

    def create_conversation(self, owner_id: str, title: str = "新会话") -> dict[str, Any]:
        conversation_id, now = _id("conv"), _now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO conversations(id, owner_id, title, created_at, updated_at) VALUES(?,?,?,?,?)",
                (conversation_id, owner_id, title.strip() or "新会话", now, now),
            )
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return self._row(row) or {}

    def require_conversation(self, owner_id: str, conversation_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ? AND owner_id = ?",
                (conversation_id, owner_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFound("Conversation not found")
        return self._row(row) or {}

    def require_conversation_admin(self, conversation_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row is None:
            raise ResourceNotFound("Conversation not found")
        return self._row(row) or {}

    def list_conversations(self, owner_id: str, include_hidden: bool = False) -> list[dict[str, Any]]:
        query = "SELECT id, owner_id, title, hidden, created_at, updated_at FROM conversations WHERE owner_id = ?"
        params: list[Any] = [owner_id]
        if not include_hidden:
            query += " AND hidden = 0"
        query += " ORDER BY updated_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def set_conversation_hidden(self, owner_id: str, conversation_id: str, hidden: bool) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET hidden = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
                (int(hidden), _now(), conversation_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ResourceNotFound("Conversation not found")

    def rename_conversation(self, owner_id: str, conversation_id: str, title: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
                (title.strip() or "新会话", _now(), conversation_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ResourceNotFound("Conversation not found")

    def save_conversation_state(
        self, owner_id: str, conversation_id: str, snapshot: dict, plan: dict | None
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET snapshot_json = ?, plan_json = ?, updated_at = ? "
                "WHERE id = ? AND owner_id = ?",
                (json.dumps(snapshot, ensure_ascii=False), json.dumps(plan, ensure_ascii=False) if plan else None,
                 _now(), conversation_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ResourceNotFound("Conversation not found")

    def add_message(self, owner_id: str, conversation_id: str, role: str, content: str) -> dict[str, Any]:
        self.require_conversation(owner_id, conversation_id)
        message_id, now = _id("msg"), _now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO messages(id, conversation_id, owner_id, role, content, created_at) VALUES(?,?,?,?,?,?)",
                (message_id, conversation_id, owner_id, role, content, now),
            )
            connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
            row = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._row(row) or {}

    def list_messages(self, owner_id: str, conversation_id: str) -> list[dict[str, Any]]:
        self.require_conversation(owner_id, conversation_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE owner_id = ? AND conversation_id = ? ORDER BY created_at",
                (owner_id, conversation_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_file(
        self, owner_id: str, kind: str, relative_path: str, original_name: str,
        size_bytes: int, task_id: str | None = None
    ) -> dict[str, Any]:
        file_id = _id("file")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO files(id, owner_id, task_id, kind, relative_path, original_name, size_bytes, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (file_id, owner_id, task_id, kind, relative_path, original_name, size_bytes, _now()),
            )
            row = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return self._row(row) or {}

    def require_file(self, owner_id: str, file_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM files WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
                (file_id, owner_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFound("File not found")
        return self._row(row) or {}

    def create_run(self, owner_id: str, conversation_id: str, prompt: str) -> dict[str, Any]:
        run_id = _id("run")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT 1 FROM runs WHERE conversation_id = ? AND status = 'running' LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active:
                raise sqlite3.IntegrityError("conversation already has a running Agent run")
            connection.execute(
                "INSERT INTO runs(id, owner_id, conversation_id, prompt, status, started_at) VALUES(?,?,?,?,?,?)",
                (run_id, owner_id, conversation_id, prompt, "running", _now()),
            )
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row(row) or {}

    def require_run(self, owner_id: str, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ? AND owner_id = ?", (run_id, owner_id)
            ).fetchone()
        if row is None:
            raise ResourceNotFound("Run not found")
        return self._row(row) or {}

    def active_run(self, owner_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE owner_id = ? AND conversation_id = ? "
                "AND status = 'running' ORDER BY started_at DESC LIMIT 1",
                (owner_id, conversation_id),
            ).fetchone()
        return self._row(row)

    def add_run_event(self, run_id: str, event_type: str, payload: Any) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        now = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO run_events(run_id, event_type, payload_json, created_at) "
                "VALUES(?,?,?,?)",
                (run_id, event_type, encoded, now),
            )
            row = connection.execute(
                "SELECT * FROM run_events WHERE sequence = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._row(row) or {}

    def list_run_events(self, owner_id: str, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        self.require_run(owner_id, run_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT e.sequence, e.event_type, e.payload_json, e.created_at "
                "FROM run_events e JOIN runs r ON r.id = e.run_id "
                "WHERE e.run_id = ? AND r.owner_id = ? AND e.sequence > ? "
                "ORDER BY e.sequence",
                (run_id, owner_id, int(after)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json"))
            except (TypeError, json.JSONDecodeError):
                item["payload"] = {}
                item.pop("payload_json", None)
            result.append(item)
        return result

    def fail_running_runs(self, reason: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status='failed', error_text=?, finished_at=? "
                "WHERE status='running'",
                (reason, _now()),
            )
            return cursor.rowcount

    def finish_run(
        self,
        run_id: str,
        response: str,
        status: str,
        duration_ms: int,
        error: str | None = None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET response = ?, status = ?, duration_ms = ?, error_text = ?, "
                "input_tokens = ?, output_tokens = ?, finished_at = ? WHERE id = ?",
                (
                    response,
                    status,
                    duration_ms,
                    error,
                    input_tokens,
                    output_tokens,
                    _now(),
                    run_id,
                ),
            )

    def add_invocation(self, run_id: str, kind: str, name: str, status: str,
                       request_data: Any = None, response_data: Any = None, duration_ms: int | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO invocations(id, run_id, kind, name, status, request_json, response_json, duration_ms, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (_id("inv"), run_id, kind, name, status,
                 json.dumps(request_data, ensure_ascii=False, default=str) if request_data is not None else None,
                 json.dumps(response_data, ensure_ascii=False, default=str) if response_data is not None else None,
                 duration_ms, _now()),
            )

    def create_task(self, owner_id: str, conversation_id: str, name: str, requested: str,
                    resolved: str, reason: str, plan: dict, retry_of: str | None = None,
                    *, resource_class: str = "cpu", requested_cpus: float = 1,
                    requested_gpus: float = 0) -> dict[str, Any]:
        task_id = _id("task")
        with self.connect() as connection:
            sequence = int(connection.execute(
                "SELECT COALESCE(MAX(queue_sequence), 0) + 1 FROM tasks"
            ).fetchone()[0])
            connection.execute(
                "INSERT INTO tasks(id, owner_id, conversation_id, name, status, requested_executor, "
                "resolved_executor, recommendation_reason, plan_json, retry_of, resource_class, "
                "requested_cpus, requested_gpus, queue_reason, queue_sequence, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, owner_id, conversation_id, name, "queued", requested, resolved, reason,
                 json.dumps(plan, ensure_ascii=False), retry_of, resource_class, requested_cpus,
                 requested_gpus, "waiting_dispatch", sequence, _now()),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row(row) or {}

    def require_task(self, owner_id: str, task_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ? AND owner_id = ?", (task_id, owner_id)
            ).fetchone()
        if row is None:
            raise ResourceNotFound("Task not found")
        return self._row(row) or {}

    def require_task_admin(self, task_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ResourceNotFound("Task not found")
        return self._row(row) or {}

    def list_tasks(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT t.*, u.username, u.display_name FROM tasks t JOIN users u ON u.id = t.owner_id"
        params: tuple[Any, ...] = ()
        if owner_id is not None:
            query += " WHERE t.owner_id = ?"
            params = (owner_id,)
        query += " ORDER BY t.created_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_task(self, task_id: str, **values: Any) -> None:
        allowed = {"status", "started_at", "finished_at", "cancel_requested", "ray_job_id",
                   "result_relative_path", "log_text", "error_text", "queue_reason",
                   "assigned_gpu_ids", "dispatch_attempt", "scheduler_owner",
                   "lease_expires_at", "heartbeat_at", "external_id", "runtime_metadata"}
        updates, params = [], []
        for key, value in values.items():
            if key not in allowed:
                raise ValueError(f"Unsupported task field: {key}")
            updates.append(f"{key} = ?")
            params.append(value)
        if not updates:
            return
        params.append(task_id)
        with self.connect() as connection:
            cursor = connection.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
            if cursor.rowcount != 1:
                raise ResourceNotFound("Task not found")

    def queued_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status = 'queued' "
                "ORDER BY queue_sequence, created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def active_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status IN ('dispatching','running','cancelling')"
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_task(self, task_id: str, scheduler_owner: str, lease_expires_at: str,
                   assigned_gpu_ids: list[int]) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status='dispatching', queue_reason=NULL, scheduler_owner=?, "
                "lease_expires_at=?, assigned_gpu_ids=?, dispatch_attempt=dispatch_attempt+1 "
                "WHERE id=? AND status='queued' AND cancel_requested=0",
                (scheduler_owner, lease_expires_at, json.dumps(assigned_gpu_ids), task_id),
            )
            return cursor.rowcount == 1

    def cancel_queued_task(self, task_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status='cancelled', cancel_requested=1, queue_reason=NULL, "
                "finished_at=? WHERE id=? AND status='queued'",
                (_now(), task_id),
            )
            return cursor.rowcount == 1

    def recover_dispatching_tasks(self, now: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status='queued', queue_reason='recovering', scheduler_owner=NULL, "
                "lease_expires_at=NULL, assigned_gpu_ids=NULL "
                "WHERE status='dispatching' AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
                (now,),
            )
            return cursor.rowcount

    def get_scheduler_state(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM scheduler_state WHERE key=?", (key,)
            ).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def set_scheduler_state(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO scheduler_state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _now()),
            )

    def acquire_scheduler_lease(self, owner: str, expires_at: str, now: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE scheduler_lease SET owner=?, expires_at=? WHERE singleton=1 "
                "AND (owner=? OR owner IS NULL OR expires_at IS NULL OR expires_at < ?)",
                (owner, expires_at, owner, now),
            )
            return cursor.rowcount == 1

    def release_scheduler_lease(self, owner: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE scheduler_lease SET owner=NULL, expires_at=NULL "
                "WHERE singleton=1 AND owner=?",
                (owner,),
            )

    def list_runs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT r.*, u.username, u.display_name, c.title conversation_title, c.hidden conversation_hidden "
                "FROM runs r JOIN users u ON u.id = r.owner_id "
                "JOIN conversations c ON c.id = r.conversation_id ORDER BY r.started_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def run_detail(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT r.*, u.username, u.display_name, c.title conversation_title "
                "FROM runs r JOIN users u ON u.id = r.owner_id JOIN conversations c ON c.id = r.conversation_id "
                "WHERE r.id = ?", (run_id,)
            ).fetchone()
            invocations = connection.execute(
                "SELECT * FROM invocations WHERE run_id = ? ORDER BY created_at", (run_id,)
            ).fetchall()
            events = connection.execute(
                "SELECT sequence, event_type, payload_json, created_at FROM run_events "
                "WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        if row is None:
            raise ResourceNotFound("Run not found")
        result = dict(row)
        result["invocations"] = [dict(item) for item in invocations]
        result["events"] = [
            {**dict(item), "payload": json.loads(item["payload_json"])} for item in events
        ]
        for item in result["events"]:
            item.pop("payload_json", None)
        return result

    def overview(self) -> dict[str, Any]:
        with self.connect() as connection:
            return {
                "users": int(connection.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]),
                "runs": int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
                "run_success": int(connection.execute("SELECT COUNT(*) FROM runs WHERE status='success'").fetchone()[0]),
                "tasks": int(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]),
                "running_tasks": int(connection.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('dispatching','running','cancelling')").fetchone()[0]),
                "failed_tasks": int(connection.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'").fetchone()[0]),
            }

    def audit(self, actor_id: str, action: str, resource_type: str,
              resource_id: str | None = None, details: Any = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_events(id, actor_id, action, resource_type, resource_id, details_json, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (_id("audit"), actor_id, action, resource_type, resource_id,
                 json.dumps(details, ensure_ascii=False, default=str) if details is not None else None, _now()),
            )
