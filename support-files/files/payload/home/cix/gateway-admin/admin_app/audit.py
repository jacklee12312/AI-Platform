"""Durable, bounded management audit records (no inference or Docker access)."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


PENDING = ("accepted", "queued", "running")
TERMINAL = ("succeeded", "failed", "unknown")
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SENSITIVE_FIELD = re.compile(
    r"password|passwd|secret|authorization|cookie|csrf|api[_-]?key$|(?:^|[_-])token$", re.I
)


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if SENSITIVE_FIELD.search(str(k)) else redact(v, secrets)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
    value = re.sub(
        r"(?i)(?<![\w-])((?:[\w-]{0,64}(?:password|passwd|secret|api[_-]?key|token)|authorization)"
        r"[\"']?\s*(?:[:=]\s*|\s+))[\"']?[^\s,;\"']+",
        r"\1[REDACTED]", value,
    )
    return value


class AuditStore:
    def __init__(self, path: Path, secrets: tuple[str, ...] = ()):
        self.path = path
        self.secrets = secrets

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            # sqlite's context manager commits on success, rolls back on error;
            # it does NOT close the connection itself.
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS audit (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
                request_id TEXT NOT NULL, detail TEXT NOT NULL,
                job_id TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
            )""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(audit)")}
            for column in ("job_id", "updated_at"):
                if column not in columns:
                    connection.execute(f"ALTER TABLE audit ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            # Preserve the old schema/data and recover any previously committed jobs.
            for row in connection.execute("SELECT id, detail FROM audit WHERE job_id = '' AND status = 'accepted'").fetchall():
                try:
                    job_id = json.loads(row["detail"]).get("job_id", "")
                except (ValueError, AttributeError):
                    continue
                if isinstance(job_id, str) and JOB_ID.fullmatch(job_id):
                    connection.execute("UPDATE audit SET job_id = ? WHERE id = ?", (job_id, row["id"]))
            connection.execute("UPDATE audit SET updated_at = created_at WHERE updated_at = ''")
            for column in ("request_id", "job_id", "created_at", "status"):
                connection.execute(f"CREATE INDEX IF NOT EXISTS audit_{column} ON audit ({column})")

    def _detail(self, detail: Any) -> str:
        value = json.dumps(redact(detail, self.secrets), ensure_ascii=False, separators=(",", ":"))
        if len(value) > 8000:
            value = json.dumps({"truncated": True, "preview": value[:3000]}, ensure_ascii=False)
        return value

    def write(self, action: str, target: str, status: str, request_id: str,
              detail: Any = "", *, job_id: str = "", actor: str = "admin") -> None:
        if job_id and not JOB_ID.fullmatch(job_id):
            raise ValueError("invalid job id")
        stamp = now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO audit (id, created_at, actor, action, target, status, request_id, detail, job_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, stamp, actor, action, redact(target, self.secrets), status,
                 redact(request_id, self.secrets), self._detail(detail), job_id, stamp),
            )

    def pending(self) -> list[str]:
        with self.connection() as connection:
            return [row[0] for row in connection.execute(
                "SELECT DISTINCT job_id FROM audit WHERE job_id != '' AND status IN ('accepted','queued','running')"
            )]

    def record_job(self, snapshot: dict[str, Any]) -> None:
        status = snapshot.get("status")
        job_id = snapshot.get("id")
        if status not in (*PENDING, *TERMINAL) or not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError("invalid job snapshot")
        safe_snapshot = {key: snapshot[key] for key in (
            "id", "kind", "target", "status", "created_at", "started_at", "finished_at", "result", "error"
        ) if key in snapshot}
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, status, detail FROM audit WHERE job_id = ? AND status IN ('accepted','queued','running','unknown')",
                (job_id,),
            ).fetchall()
            for row in rows:
                # Concurrent browser/background polls must never regress progress.
                if row["status"] == "unknown" and status not in ("succeeded", "failed"):
                    continue
                if row["status"] == "running" and status in ("accepted", "queued"):
                    continue
                detail = json.loads(row["detail"]) if row["detail"] else {}
                if not isinstance(detail, dict):
                    detail = {"submission": detail}
                detail["job"] = safe_snapshot
                connection.execute("UPDATE audit SET status = ?, detail = ?, updated_at = ? WHERE id = ?",
                                   (status, self._detail(detail), now(), row["id"]))

    def read(self, *, limit: int = 100, request_id: str = "", job_id: str = "", q: str = "") -> list[dict[str, Any]]:
        clauses, values = [], []
        for column, value in (("request_id", request_id), ("job_id", job_id)):
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        if q:
            clauses.append("(" + " OR ".join(f"instr(lower({c}), lower(?)) > 0" for c in
                           ("action", "target", "status", "request_id", "job_id", "detail")) + ")")
            values.extend([q] * 6)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM audit" + where + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                      (*values, max(1, min(limit, 500)))).fetchall()
        return [dict(row) for row in rows]
