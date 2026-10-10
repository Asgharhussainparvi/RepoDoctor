"""SQLite persistence for debugging sessions (stdlib only).

Session state, proposed patches and approvals live here, so a WAITING_APPROVAL
session survives a server restart. State changes use compare-and-set so two
callers can never both move a session from the same state.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .schemas import AgentEvent, AttemptFeedback
from .states import State

SCHEMA = """
CREATE TABLE IF NOT EXISTS debug_sessions (
    id TEXT PRIMARY KEY, repo_path TEXT NOT NULL, state TEXT NOT NULL,
    patch_attempts INTEGER NOT NULL DEFAULT 0, active_seconds REAL NOT NULL DEFAULT 0,
    reproduction_json TEXT, hypothesis_json TEXT, feedback_json TEXT NOT NULL DEFAULT '[]',
    message TEXT NOT NULL DEFAULT '', error_code TEXT, report_json TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, state TEXT NOT NULL,
    kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, tool TEXT NOT NULL,
    ok INTEGER NOT NULL, error_code TEXT, duration_seconds REAL NOT NULL,
    budgeted INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, attempt INTEGER NOT NULL,
    sha256 TEXT NOT NULL, diff TEXT NOT NULL, summary TEXT NOT NULL, files_json TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT,
    UNIQUE (session_id, sha256)
);
CREATE TABLE IF NOT EXISTS verification_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, attempt INTEGER NOT NULL,
    verified INTEGER NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class StaleStateError(Exception):
    """The session was not in the state the caller expected (a concurrent change won)."""


@dataclass(frozen=True)
class SessionRow:
    id: str
    repo_path: str
    state: State
    patch_attempts: int
    active_seconds: float
    reproduction_json: str | None
    hypothesis_json: str | None
    feedback: list[AttemptFeedback]
    message: str
    error_code: str | None
    report_json: str | None


@dataclass(frozen=True)
class PatchRow:
    session_id: str
    attempt: int
    sha256: str
    diff: str
    summary: str
    files: list[str]
    status: str  # proposed -> approved -> applied -> verified | failed_verification; or rejected / apply_failed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _run(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- sessions ----
    def create_session(self, repo_path: str) -> str:
        sid = uuid.uuid4().hex[:12]
        now = _now()
        self._run(
            "INSERT INTO debug_sessions (id, repo_path, state, created_at, updated_at) VALUES (?,?,?,?,?)",
            (sid, repo_path, State.INITIALIZE.value, now, now),
        )
        return sid

    def get_session(self, sid: str) -> SessionRow:
        row = self._one("SELECT * FROM debug_sessions WHERE id=?", (sid,))
        if row is None:
            raise KeyError(sid)
        return SessionRow(
            id=row["id"], repo_path=row["repo_path"], state=State(row["state"]),
            patch_attempts=row["patch_attempts"], active_seconds=row["active_seconds"],
            reproduction_json=row["reproduction_json"], hypothesis_json=row["hypothesis_json"],
            feedback=[AttemptFeedback(**f) for f in json.loads(row["feedback_json"])],
            message=row["message"], error_code=row["error_code"], report_json=row["report_json"],
        )

    def set_state(self, sid: str, *, expected: State, new: State, message: str = "",
                  error_code: str | None = None) -> None:
        cur = self._run(
            "UPDATE debug_sessions SET state=?, message=?, error_code=?, updated_at=? WHERE id=? AND state=?",
            (new.value, message, error_code, _now(), sid, expected.value),
        )
        if cur.rowcount != 1:
            raise StaleStateError(f"session {sid} is no longer in {expected.value}")

    def _set_field(self, sid: str, column: str, value: object) -> None:
        assert column in {"reproduction_json", "hypothesis_json", "report_json"}
        self._run(f"UPDATE debug_sessions SET {column}=?, updated_at=? WHERE id=?", (value, _now(), sid))

    def save_reproduction(self, sid: str, payload: str) -> None:
        self._set_field(sid, "reproduction_json", payload)

    def save_hypothesis(self, sid: str, payload: str) -> None:
        self._set_field(sid, "hypothesis_json", payload)

    def save_report(self, sid: str, payload: str) -> None:
        self._set_field(sid, "report_json", payload)

    def increment_attempts(self, sid: str) -> int:
        with self._lock, self._conn:
            self._conn.execute("UPDATE debug_sessions SET patch_attempts = patch_attempts + 1 WHERE id=?", (sid,))
            return self._conn.execute("SELECT patch_attempts FROM debug_sessions WHERE id=?", (sid,)).fetchone()[0]

    def add_active_seconds(self, sid: str, seconds: float) -> None:
        self._run("UPDATE debug_sessions SET active_seconds = active_seconds + ? WHERE id=?", (max(0.0, seconds), sid))

    def append_feedback(self, sid: str, feedback: AttemptFeedback) -> None:
        with self._lock, self._conn:
            raw = self._conn.execute("SELECT feedback_json FROM debug_sessions WHERE id=?", (sid,)).fetchone()[0]
            items = json.loads(raw)
            items.append(feedback.model_dump())
            self._conn.execute("UPDATE debug_sessions SET feedback_json=? WHERE id=?", (json.dumps(items), sid))

    # ---- events and tool calls ----
    def add_step(self, sid: str, state: State, kind: str, message: str) -> AgentEvent:
        now = _now()
        cur = self._run(
            "INSERT INTO agent_steps (session_id, state, kind, message, created_at) VALUES (?,?,?,?,?)",
            (sid, state.value, kind, message, now),
        )
        return AgentEvent(seq=cur.lastrowid, session_id=sid, state=state, kind=kind, message=message, created_at=now)

    def steps(self, sid: str) -> list[AgentEvent]:
        return [
            AgentEvent(seq=r["id"], session_id=sid, state=State(r["state"]), kind=r["kind"],
                       message=r["message"], created_at=r["created_at"])
            for r in self._all("SELECT * FROM agent_steps WHERE session_id=? ORDER BY id", (sid,))
        ]

    def add_tool_calls(self, sid: str, records: Sequence) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO tool_calls (session_id, tool, ok, error_code, duration_seconds, budgeted, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                [(sid, r.tool, int(r.ok), r.error_code, r.duration_seconds, int(r.budgeted), now) for r in records],
            )

    def tool_calls(self, sid: str) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM tool_calls WHERE session_id=? ORDER BY id", (sid,))]

    # ---- patches and approval ----
    @staticmethod
    def _patch(row: sqlite3.Row) -> PatchRow:
        return PatchRow(row["session_id"], row["attempt"], row["sha256"], row["diff"], row["summary"],
                        json.loads(row["files_json"]), row["status"])

    def add_patch(self, sid: str, *, attempt: int, sha256: str, diff: str, summary: str, files: list[str]) -> None:
        self._run(
            "INSERT INTO patches (session_id, attempt, sha256, diff, summary, files_json, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sid, attempt, sha256, diff, summary, json.dumps(files), "proposed", _now()),
        )

    def has_patch(self, sid: str, sha256: str) -> bool:
        return self._one("SELECT 1 FROM patches WHERE session_id=? AND sha256=?", (sid, sha256)) is not None

    def get_patch(self, sid: str, sha256: str) -> PatchRow | None:
        row = self._one("SELECT * FROM patches WHERE session_id=? AND sha256=?", (sid, sha256))
        return self._patch(row) if row else None

    def latest_patch(self, sid: str, status: str | None = None) -> PatchRow | None:
        sql, params = "SELECT * FROM patches WHERE session_id=?", [sid]
        if status:
            sql, params = sql + " AND status=?", params + [status]
        row = self._one(sql + " ORDER BY id DESC LIMIT 1", params)
        return self._patch(row) if row else None

    def set_patch_status(self, sid: str, sha256: str, *, expected: str, new: str) -> None:
        cur = self._run(
            "UPDATE patches SET status=?, decided_at=? WHERE session_id=? AND sha256=? AND status=?",
            (new, _now(), sid, sha256, expected),
        )
        if cur.rowcount != 1:
            raise StaleStateError(f"patch {sha256[:12]} is not {expected}")

    def approve_patch(self, sid: str, sha256: str) -> None:
        """Atomically: patch proposed -> approved AND session WAITING_APPROVAL -> APPLY_PATCH."""
        with self._lock, self._conn:
            moved = self._conn.execute(
                "UPDATE debug_sessions SET state=?, message=?, updated_at=? WHERE id=? AND state=?",
                (State.APPLY_PATCH.value, "Patch approved", _now(), sid, State.WAITING_APPROVAL.value),
            ).rowcount
            if moved != 1:
                raise StaleStateError("session is not waiting for approval")
            marked = self._conn.execute(
                "UPDATE patches SET status='approved', decided_at=? WHERE session_id=? AND sha256=? AND status='proposed'",
                (_now(), sid, sha256),
            ).rowcount
            if marked != 1:
                raise KeyError("no pending patch with this hash")  # rolls back the state change too

    def reject_patch(self, sid: str, sha256: str) -> None:
        with self._lock, self._conn:
            moved = self._conn.execute(
                "UPDATE debug_sessions SET state=?, message=?, updated_at=? WHERE id=? AND state=?",
                (State.REJECTED.value, "Patch rejected. No changes were applied.", _now(), sid,
                 State.WAITING_APPROVAL.value),
            ).rowcount
            if moved != 1:
                raise StaleStateError("session is not waiting for approval")
            marked = self._conn.execute(
                "UPDATE patches SET status='rejected', decided_at=? WHERE session_id=? AND sha256=? AND status='proposed'",
                (_now(), sid, sha256),
            ).rowcount
            if marked != 1:
                raise KeyError("no pending patch with this hash")

    # ---- verification ----
    def save_verification(self, sid: str, attempt: int, verified: bool, payload: str) -> None:
        self._run(
            "INSERT INTO verification_results (session_id, attempt, verified, result_json, created_at) VALUES (?,?,?,?,?)",
            (sid, attempt, int(verified), payload, _now()),
        )

    def latest_verification_json(self, sid: str) -> str | None:
        row = self._one("SELECT result_json FROM verification_results WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,))
        return row["result_json"] if row else None
