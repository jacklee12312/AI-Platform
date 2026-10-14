from __future__ import annotations

import hashlib
import csv
import io
import json
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .usage_quality import FIELDS, usage_details, utc_boundary
from .access import KeyPolicy, expired
from .access_store import AccessStore
from .request_diagnostics import (
    REASONS,
    clean_diagnostics,
    clean_request_content,
    describe_diagnostics,
    initialize_diagnostics,
    unavailable_request_content,
)
from .request_content import clean_request_metadata, request_summary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_usage(value: Any) -> tuple[int, int, int, bool]:
    detail = usage_details(value)
    return (*(detail["tokens"][field] or 0 for field in FIELDS), detail["reported"])


def _aggregates(alias: str = "u") -> str:
    fields = ["COUNT(" + alias + ".id) AS request_count"]
    for field in FIELDS:
        known = field.replace("_tokens", "_known")
        fields += [f"COALESCE(SUM(CASE WHEN {alias}.{known}=1 THEN {alias}.{field} ELSE 0 END),0) AS {field}",
                   f"COALESCE(SUM(CASE WHEN {alias}.id IS NOT NULL AND {alias}.{known}=0 THEN 1 ELSE 0 END),0) AS missing_{field}_requests"]
    fields += [f"COALESCE(SUM(CASE WHEN {alias}.usage_reported=0 THEN 1 ELSE 0 END),0) AS unreported_requests",
               f"COALESCE(SUM(CASE WHEN {alias}.error_code IN ('model_concurrency_exceeded','key_concurrency_exceeded','global_concurrency_exceeded','pool_concurrency_exceeded','rate_limit_exceeded','daily_quota_exceeded','api_key_expired','model_not_allowed') THEN 1 ELSE 0 END),0) AS admission_rejected_requests",
               f"COALESCE(SUM(CASE WHEN {alias}.usage_quality IN ('partial','invalid','inconsistent') THEN 1 ELSE 0 END),0) AS anomaly_requests",
               f"COALESCE(SUM(CASE WHEN {alias}.usage_quality='legacy' THEN 1 ELSE 0 END),0) AS legacy_requests"]
    return ", ".join(fields)


class APIKeyUsageStore(AccessStore):
    """Persistent API-key registry and token-usage ledger.

    Only SHA-256 digests are stored. Newly generated plaintext keys are returned
    once to the caller and cannot be recovered from the database afterwards.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._schema_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self, bootstrap_key: str = "") -> None:
        with self._schema_lock:
            if not self._initialized:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self._db() as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS api_keys (
                            id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            key_prefix TEXT NOT NULL,
                            key_hash TEXT NOT NULL UNIQUE,
                            created_at TEXT NOT NULL,
                            last_used_at TEXT,
                            enabled INTEGER NOT NULL DEFAULT 1,
                            revoked_at TEXT
                        );
                        CREATE TABLE IF NOT EXISTS usage_events (
                            id TEXT PRIMARY KEY,
                            api_key_id TEXT NOT NULL,
                            request_id TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            endpoint TEXT NOT NULL,
                            model TEXT NOT NULL,
                            input_tokens INTEGER NOT NULL DEFAULT 0,
                            output_tokens INTEGER NOT NULL DEFAULT 0,
                            total_tokens INTEGER NOT NULL DEFAULT 0,
                            usage_reported INTEGER NOT NULL DEFAULT 0,
                            status TEXT NOT NULL,
                            FOREIGN KEY(api_key_id) REFERENCES api_keys(id)
                        );
                        CREATE INDEX IF NOT EXISTS idx_usage_key_created
                            ON usage_events(api_key_id, created_at);
                        CREATE INDEX IF NOT EXISTS idx_usage_created
                            ON usage_events(created_at);
                        CREATE INDEX IF NOT EXISTS idx_usage_model
                            ON usage_events(model);
                        """
                    )
                    # Make additive migration and legacy backfill one transaction.
                    connection.execute("BEGIN IMMEDIATE")
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_events)")}
                    for field in ("input_known", "output_known", "total_known"):
                        if field not in columns:
                            connection.execute(f"ALTER TABLE usage_events ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0")
                            connection.execute(f"UPDATE usage_events SET {field}=usage_reported")
                    for field, default in (("usage_quality", "legacy"), ("usage_source", "legacy"),
                                           ("usage_raw", "{}"), ("usage_issues", "[]")):
                        if field not in columns:
                            connection.execute(f"ALTER TABLE usage_events ADD COLUMN {field} TEXT NOT NULL DEFAULT '{default}'")
                    self._initialize_access(connection)
                    initialize_diagnostics(connection)
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_events)")}
                    if "request_content" not in columns:
                        connection.execute(
                            "ALTER TABLE usage_events ADD COLUMN request_content TEXT NOT NULL DEFAULT '{}'"
                        )
                    if "request_metadata" not in columns:
                        connection.execute(
                            "ALTER TABLE usage_events ADD COLUMN request_metadata TEXT NOT NULL DEFAULT '{}'"
                        )
                self._initialized = True
            if bootstrap_key:
                digest = _key_hash(bootstrap_key)
                with self._db() as connection:
                    existing = connection.execute(
                        "SELECT id FROM api_keys WHERE key_hash = ?", (digest,)
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO api_keys
                                (id, name, key_prefix, key_hash, created_at, enabled)
                            VALUES (?, ?, ?, ?, ?, 1)
                            """,
                            (
                                uuid.uuid4().hex,
                                "默认兼容 Key",
                                bootstrap_key[:12],
                                digest,
                                _now(),
                            ),
                        )

    def enabled_key_count(self) -> int:
        with self._db() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM api_keys WHERE enabled = 1 AND revoked_at IS NULL"
            ).fetchone()
        return int(row["count"])

    def authenticate(self, plaintext_key: str, *, include_expired: bool = False) -> dict[str, Any] | None:
        if not plaintext_key:
            return None
        digest = _key_hash(plaintext_key)
        with self._db() as connection:
            row = connection.execute(
                """
                SELECT id, name, key_prefix, policy
                FROM api_keys
                WHERE key_hash = ? AND enabled = 1 AND revoked_at IS NULL
                """,
                (digest,),
            ).fetchone()
            if row is None:
                return None
            policy = KeyPolicy.model_validate_json(row["policy"]).model_dump()
            if expired(policy) and not include_expired:
                return None
            connection.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_now(), row["id"])
            )
        return {**dict(row), "policy": policy}

    def create_key(self, name: str, *, policy: dict | None = None) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("API Key 名称不能为空")
        clean_policy = KeyPolicy.model_validate(policy or {}).model_dump()
        plaintext = f"mdk_{secrets.token_urlsafe(32)}"
        created_at = _now()
        key_id = uuid.uuid4().hex
        prefix = plaintext[:12]
        with self._db() as connection:
            connection.execute(
                """
                INSERT INTO api_keys
                    (id, name, key_prefix, key_hash, created_at, enabled, policy)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (key_id, clean_name, prefix, _key_hash(plaintext), created_at, json.dumps(clean_policy)),
            )
        return {
            "id": key_id,
            "name": clean_name,
            "prefix": prefix,
            "key": plaintext,
            "created_at": created_at,
            "enabled": True,
            "policy": clean_policy,
        }

    def update_key(self, key_id: str, *, name: str | None = None, enabled: bool | None = None,
                   policy: dict | None = None) -> dict[str, Any]:
        clean_policy = KeyPolicy.model_validate(policy).model_dump(exclude_unset=True) if policy is not None else None
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, revoked_at, policy FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(key_id)
            if existing["revoked_at"] is not None:
                raise ValueError("已撤销的 API Key 不能修改")
            assignments: list[str] = []
            values: list[Any] = []
            if clean_policy is not None:
                clean_policy = KeyPolicy.model_validate({**json.loads(existing["policy"]), **clean_policy}).model_dump()
                assignments.append("policy = ?")
                values.append(json.dumps(clean_policy))
            if name is not None:
                clean_name = name.strip()
                if not clean_name:
                    raise ValueError("API Key 名称不能为空")
                assignments.append("name = ?")
                values.append(clean_name)
            if enabled is not None:
                assignments.append("enabled = ?")
                values.append(1 if enabled else 0)
            if assignments:
                values.append(key_id)
                connection.execute(
                    f"UPDATE api_keys SET {', '.join(assignments)} WHERE id = ?", values
                )
        return self.get_key(key_id)

    def revoke_key(self, key_id: str) -> dict[str, Any]:
        revoked_at = _now()
        with self._db() as connection:
            cursor = connection.execute(
                """
                UPDATE api_keys
                SET enabled = 0, revoked_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (revoked_at, key_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(key_id)
        return self.get_key(key_id)

    def get_key(self, key_id: str) -> dict[str, Any]:
        keys = self.list_keys(key_id=key_id)
        if not keys:
            raise KeyError(key_id)
        return keys[0]

    def list_keys(self, *, key_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE k.id = ?" if key_id else ""
        parameters: tuple[Any, ...] = (key_id,) if key_id else ()
        with self._db() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    k.id, k.name, k.key_prefix AS prefix, k.created_at,
                    k.last_used_at, k.enabled, k.revoked_at, k.policy,
                    {_aggregates()}
                FROM api_keys k
                LEFT JOIN usage_events u ON u.api_key_id = k.id
                {where}
                GROUP BY k.id
                ORDER BY k.created_at ASC
                """,
                parameters,
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["enabled"] = bool(value["enabled"])
            value["policy"] = KeyPolicy.model_validate_json(value["policy"]).model_dump()
            value["access"] = self.key_access_summary(value["id"], value["policy"])
            result.append(value)
        return result

    def record_usage(
        self,
        *,
        api_key_id: str | None,
        request_id: str,
        endpoint: str,
        model: str,
        usage: Any,
        status: str = "succeeded",
        event_id: str | None = None,
        diagnostics: dict | None = None,
        request_content: dict | None = None,
        request_metadata: dict | None = None,
    ) -> None:
        if not api_key_id:
            return
        detail = usage_details(usage, endpoint)
        diagnostics = clean_diagnostics(diagnostics)
        request_content = clean_request_content(request_content)
        request_metadata = clean_request_metadata(request_metadata)
        input_tokens, output_tokens, total_tokens = (detail["tokens"][field] or 0 for field in FIELDS)
        with self._db() as connection:
            connection.execute(
                """
                INSERT INTO usage_events
                    (id, api_key_id, request_id, created_at, endpoint, model,
                     input_tokens, output_tokens, total_tokens, usage_reported, status,
                     input_known,output_known,total_known,usage_quality,usage_source,usage_raw,usage_issues,
                     error_code,diagnostics,request_content,request_metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    event_id or uuid.uuid4().hex,
                    api_key_id,
                    request_id,
                    _now(),
                    endpoint,
                    model or "unknown",
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    1 if detail["reported"] else 0,
                    status,
                    *(int(detail["tokens"][field] is not None) for field in FIELDS),
                    detail["quality"], detail["source"],
                    json.dumps({"values": detail["raw"], "fields": detail["sources"]}, ensure_ascii=False),
                    json.dumps(detail["issues"]),
                    diagnostics.get("code", ""),
                    json.dumps(diagnostics, ensure_ascii=False),
                    json.dumps(request_content, ensure_ascii=False),
                    json.dumps(request_metadata, ensure_ascii=False),
                ),
            )

    def usage_report(
        self,
        *,
        key_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
        offset: int = 0,
        model: str | None = None,
        request_id: str | None = None,
        status: str | None = None,
        error_code: str | None = None,
        export_all: bool = False,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        parameters: list[Any] = []
        start, end = utc_boundary(start), utc_boundary(end)
        if start and end and start > end:
            raise ValueError("开始时间不能晚于结束时间")
        if key_id:
            conditions.append("u.api_key_id = ?")
            parameters.append(key_id)
        if start:
            conditions.append("julianday(u.created_at) >= julianday(?)")
            parameters.append(start)
        if end:
            conditions.append("julianday(u.created_at) <= julianday(?)")
            parameters.append(end)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        for column, value in (("model", model), ("request_id", request_id), ("status", status)):
            if value:
                if len(value) > 128:
                    raise ValueError("查询参数过长")
                conditions.append(f"u.{column} = ?")
                parameters.append(value)
        if error_code:
            if len(error_code) > 64:
                raise ValueError("错误码过长")
            if error_code == "historical_unknown":
                conditions.append(
                    "u.error_code='' AND u.status NOT IN ('succeeded','cancelled','interrupted')"
                )
            elif error_code in ("client_cancelled", "stream_interrupted"):
                conditions.append("(u.error_code=? OR (u.error_code='' AND u.status=?))")
                parameters.extend(
                    [
                        error_code,
                        "cancelled" if error_code == "client_cancelled" else "interrupted",
                    ]
                )
            else:
                conditions.append("u.error_code = ?")
                parameters.append(error_code)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._db() as connection:
            connection.execute("BEGIN")  # One snapshot for totals, groups and details.
            summary = dict(
                connection.execute(
                    f"""
                    SELECT {_aggregates()}
                    FROM usage_events u
                    {where}
                    """,
                    parameters,
                ).fetchone()
            )
            by_key = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT u.api_key_id, k.name, k.key_prefix AS prefix,
                           {_aggregates()}
                    FROM usage_events u
                    JOIN api_keys k ON k.id = u.api_key_id
                    {where}
                    GROUP BY u.api_key_id
                    ORDER BY total_tokens DESC, k.name ASC
                    """,
                    parameters,
                ).fetchall()
            ]
            by_model = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT u.model, {_aggregates()}
                    FROM usage_events u
                    {where}
                    GROUP BY u.model
                    ORDER BY total_tokens DESC, u.model ASC
                    """,
                    parameters,
                ).fetchall()
            ]
            daily = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT substr(u.created_at, 1, 10) AS date,
                           {_aggregates()}
                    FROM usage_events u
                    {where}
                    GROUP BY substr(u.created_at, 1, 10)
                    ORDER BY date ASC
                    """,
                    parameters,
                ).fetchall()
            ]
            limit, offset = max(1, min(limit, 500)), max(0, offset)
            recent_parameters = parameters if export_all else [*parameters, limit, offset]
            recent = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT u.id, u.request_id, u.created_at, u.endpoint, u.model,
                           u.input_tokens, u.output_tokens, u.total_tokens,
                           u.usage_reported, u.status, u.input_known, u.output_known, u.total_known,
                           u.usage_quality, u.usage_source, u.usage_raw, u.usage_issues, u.diagnostics,
                           u.request_content, u.request_metadata,
                           u.api_key_id, k.name AS key_name, k.key_prefix AS prefix
                    FROM usage_events u
                    JOIN api_keys k ON k.id = u.api_key_id
                    {where}
                    ORDER BY u.created_at DESC, u.rowid DESC
                    {'' if export_all else 'LIMIT ? OFFSET ?'}
                    """,
                    recent_parameters,
                ).fetchall()
            ]
            reason_options = dict(REASONS)
            for row in connection.execute(
                "SELECT DISTINCT error_code FROM usage_events WHERE error_code<>''"
            ):
                reason_options.setdefault(row[0], (row[0], ""))
        for row in recent:
            row["usage_reported"] = bool(row["usage_reported"])
            for field in FIELDS:
                if not row.pop(field.replace("_tokens", "_known")):
                    row[field] = None
            row["usage_raw"] = json.loads(row["usage_raw"])
            row["usage_issues"] = json.loads(row["usage_issues"])
            row["diagnostics"] = describe_diagnostics(
                json.loads(row["diagnostics"]), row["status"]
            )
            try:
                content = clean_request_content(json.loads(row.pop("request_content")))
            except (json.JSONDecodeError, TypeError):
                content = unavailable_request_content("历史记录未保存请求内容")
            try:
                row["request_metadata"] = clean_request_metadata(
                    json.loads(row["request_metadata"])
                )
            except (json.JSONDecodeError, TypeError):
                row["request_metadata"] = {}
            row["request_summary"] = request_summary(content)
            if export_all:
                row["request_content"] = content
        return {
            "filters": {"api_key_id": key_id, "start": start, "end": end, "model": model, "request_id": request_id, "status": status, "error_code": error_code},
            "reason_options": [
                {"code": code, "label": label}
                for code, (label, _) in reason_options.items()
            ],
            "pagination": {"offset": offset, "limit": limit, "total": summary["request_count"], "has_more": offset + len(recent) < summary["request_count"]},
            "accounting": {"tokens": "known_subtotals", "daily_timezone": "UTC", "management_tests_included": False},
            "summary": summary,
            "by_key": by_key,
            "by_model": by_model,
            "daily": daily,
            "recent": recent,
        }

    def request_detail(self, event_id: str) -> dict[str, Any] | None:
        with self._db() as connection:
            row = connection.execute(
                """
                SELECT u.*, k.name AS key_name, k.key_prefix AS prefix
                FROM usage_events u
                JOIN api_keys k ON k.id = u.api_key_id
                WHERE u.id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in FIELDS:
            if not result.pop(field.replace("_tokens", "_known")):
                result[field] = None
        for field, fallback in (("usage_raw", {}), ("usage_issues", [])):
            try:
                result[field] = json.loads(result[field])
            except (json.JSONDecodeError, TypeError):
                result[field] = fallback
        result["diagnostics"] = describe_diagnostics(
            json.loads(result["diagnostics"]), result["status"]
        )
        try:
            result["request_content"] = clean_request_content(
                json.loads(result["request_content"])
            )
        except (json.JSONDecodeError, TypeError):
            result["request_content"] = unavailable_request_content(
                "历史记录未保存请求内容"
            )
        try:
            result["request_metadata"] = clean_request_metadata(
                json.loads(result["request_metadata"])
            )
        except (json.JSONDecodeError, TypeError):
            result["request_metadata"] = {}
        result["request_summary"] = request_summary(result["request_content"])
        result["usage_reported"] = bool(result["usage_reported"])
        return result

    def export_usage_csv(self, **filters: Any) -> dict[str, Any]:
        report = self.usage_report(**filters, export_all=True)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "时间(UTC)",
                "Key名称",
                "模型",
                "接口",
                "结果",
                "原因",
                "错误码",
                "HTTP状态",
                "上游HTTP状态",
                "是否转交上游",
                "总耗时(ms)",
                "首次输出(ms)",
                "最后输出(ms)",
                "并发快照",
                "并发上限",
                "输入Token",
                "输出Token",
                "总Token",
                "Request ID",
                "内部记录ID",
                "信息来源",
                "请求内容(JSON)",
            ]
        )

        def safe(value: Any) -> str:
            text = "" if value is None else str(value)
            if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(
                ("\t", "\r", "\n")
            ):
                return "'" + text
            return text

        for row in report["recent"]:
            diagnostics = row["diagnostics"]
            values = [
                row["created_at"],
                row["key_name"],
                row["model"],
                row["endpoint"],
                row["status"],
                diagnostics["reason"],
                diagnostics["code"],
                diagnostics.get("http_status"),
                diagnostics.get("upstream_http_status"),
                diagnostics.get("upstream_dispatched"),
                diagnostics.get("elapsed_ms"),
                diagnostics.get("first_output_ms"),
                diagnostics.get("last_output_ms"),
                diagnostics.get("in_flight"),
                diagnostics.get("limit"),
                *[
                    None if diagnostics["not_inferred"] else row[field]
                    for field in FIELDS
                ],
                row["request_id"],
                row["id"],
                diagnostics.get("source"),
                json.dumps(row["request_content"], ensure_ascii=False),
            ]
            writer.writerow([safe(value) for value in values])
        return {"csv": "\ufeff" + output.getvalue(), "record_count": len(report["recent"])}
