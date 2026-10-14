"""Additive policy storage; accepted-request quotas are separate from token usage."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .access import BUSINESS_TZ, KeyPolicy, ResourcePolicy, authorize, expired
from .errors import GatewayError


class AccessStore:
    def _initialize_access(self, connection):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(api_keys)")}
        if "policy" not in columns:
            connection.execute("ALTER TABLE api_keys ADD COLUMN policy TEXT NOT NULL DEFAULT '{}'")
        connection.execute("CREATE TABLE IF NOT EXISTS key_daily_requests (api_key_id TEXT NOT NULL REFERENCES api_keys(id), "
                           "day TEXT NOT NULL, request_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(api_key_id, day))")
        connection.execute("CREATE TABLE IF NOT EXISTS resource_policy (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS access_rejections (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                           "api_key_id TEXT NOT NULL REFERENCES api_keys(id), request_id TEXT NOT NULL, model TEXT NOT NULL, "
                           "code TEXT NOT NULL, created_at TEXT NOT NULL)")

    def reserve_key_request(self, key_id: str, model: str) -> None:
        now = datetime.now(timezone.utc)
        day = now.astimezone(BUSINESS_TZ).date().isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            key = db.execute("SELECT policy, enabled, revoked_at FROM api_keys WHERE id=?", (key_id,)).fetchone()
            if key is None or not key["enabled"] or key["revoked_at"]:
                raise GatewayError(401, "API Key 已停用或撤销", "invalid_api_key", "authentication_error")
            policy = KeyPolicy.model_validate_json(key["policy"]).model_dump()
            authorize(policy, model)
            row = db.execute("SELECT request_count FROM key_daily_requests WHERE api_key_id=? AND day=?", (key_id, day)).fetchone()
            count = row[0] if row else 0
            limit = policy["daily_request_limit"]
            if limit and count >= limit:
                raise GatewayError(429, "该 Key 今日请求额度已用完，北京时间次日 00:00 重置", "daily_quota_exceeded",
                                   "rate_limit_error", details={"limit": limit, "used": count, "timezone": "Asia/Shanghai"})
            db.execute("INSERT INTO key_daily_requests(api_key_id,day,request_count) VALUES(?,?,1) "
                       "ON CONFLICT(api_key_id,day) DO UPDATE SET request_count=request_count+1", (key_id, day))

    def key_access_summary(self, key_id: str, policy: dict) -> dict:
        now = datetime.now(timezone.utc)
        local = now.astimezone(BUSINESS_TZ)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._db() as db:
            row = db.execute("SELECT request_count FROM key_daily_requests WHERE api_key_id=? AND day=?",
                             (key_id, local.date().isoformat())).fetchone()
            monthly = db.execute("SELECT COALESCE(SUM(CASE WHEN total_known=1 THEN total_tokens ELSE 0 END),0), "
                                 "COALESCE(SUM(CASE WHEN total_known=0 OR usage_quality IN ('legacy','partial','invalid','inconsistent') THEN 1 ELSE 0 END),0) "
                                 "FROM usage_events WHERE api_key_id=? AND julianday(created_at)>=julianday(?)",
                                 (key_id, start)).fetchone()
        used = row[0] if row else 0
        threshold = policy["monthly_token_warning"]
        return {"expired": expired(policy, now), "daily_requests": used,
                "daily_remaining": max(0, policy["daily_request_limit"] - used) if policy["daily_request_limit"] else None,
                "monthly_known_tokens": monthly[0], "monthly_uncertain_requests": monthly[1],
                "token_warning_reached": bool(threshold and monthly[0] >= threshold), "timezone": "Asia/Shanghai"}

    def get_resource_policy(self) -> dict:
        with self._db() as db:
            row = db.execute("SELECT value FROM resource_policy WHERE id=1").fetchone()
        return ResourcePolicy.model_validate_json(row[0]).model_dump() if row else ResourcePolicy().model_dump()

    def set_resource_policy(self, policy: dict) -> dict:
        value = ResourcePolicy.model_validate(policy).model_dump()
        with self._db() as db:
            db.execute("INSERT INTO resource_policy(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                       (json.dumps(value),))
        return value

    def record_rejection(self, key_id: str | None, request_id: str, model: str, code: str) -> None:
        if not key_id:
            return
        with self._db() as db:
            db.execute("INSERT INTO access_rejections(api_key_id,request_id,model,code,created_at) VALUES(?,?,?,?,?)",
                       (key_id, request_id[:128], model[:128], code[:64], datetime.now(timezone.utc).isoformat()))
            db.execute("DELETE FROM access_rejections WHERE sequence <= "
                       "(SELECT sequence FROM access_rejections ORDER BY sequence DESC LIMIT 1 OFFSET 2000)")

    def recent_rejections(self) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT r.*, k.name AS key_name FROM access_rejections r "
                                                   "JOIN api_keys k ON k.id=r.api_key_id ORDER BY sequence DESC LIMIT 50")]
