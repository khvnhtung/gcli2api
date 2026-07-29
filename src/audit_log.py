"""
Audit log for API request attempts.

Append-only table that records every upstream API attempt with structured fields
for forensic analysis, ban detection, and incident postmortem.

Feature-gated via AUDIT_LOG_ENABLED config (default: True).
"""

import asyncio
import contextvars
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from typing import Any, Dict, Optional

import aiosqlite

from log import log

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BAN_PHRASES = [
    "service has been disabled in this account for violation of terms of",
    "violation of terms of service",
]

_VALIDATION_PHRASES = [
    "validation_required",
    "validation_url",
    "verify your account",
]

_SECRET_HINTS = (
    "auth_service_token",
    "access_token",
    "refresh_token",
    "api_key",
    "api-key",
    ".env",
    "-----begin",
)

_ROLLING_5H_MIN_SECONDS = 4 * 3600
_ROLLING_5H_MAX_SECONDS = 6 * 3600
_WEEKLY_LIKE_MIN_SECONDS = 24 * 3600

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_db_path: Optional[str] = None
_enabled: bool = True
_initialized: bool = False
_init_lock = asyncio.Lock()

_raw_enabled: bool = True
_raw_dir: str = "./audit_payloads"
_raw_retention_days: int = 7
_raw_max_bytes: int = 1024 * 1024

# Batch write queue (fire-and-forget from callers)
_write_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
_writer_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Context variables for tracking audit state across the call stack
# ---------------------------------------------------------------------------

_ctx_request_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "audit_request_id", default=None
)
_ctx_attempt_no: contextvars.ContextVar[int] = contextvars.ContextVar(
    "audit_attempt_no", default=0
)
_ctx_model: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "audit_model", default=None
)
_ctx_mode: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "audit_mode", default=None
)
_ctx_streaming: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "audit_streaming", default=False
)
_ctx_max_retries: contextvars.ContextVar[int] = contextvars.ContextVar(
    "audit_max_retries", default=0
)
_ctx_start_time: contextvars.ContextVar[float] = contextvars.ContextVar(
    "audit_start_time", default=0.0
)
_ctx_request_payload: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "audit_request_payload", default=None
)
_ctx_request_payload_path: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "audit_request_payload_path", default=None
)
_ctx_request_payload_sha256: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "audit_request_payload_sha256", default=None
)
_ctx_request_payload_size: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "audit_request_payload_size", default=None
)
_ctx_meta: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "audit_meta", default=None
)


def set_audit_context(
    mode: str,
    model: Optional[str] = None,
    streaming: bool = False,
    max_retries: int = 0,
    request_payload: Optional[Any] = None,
) -> str:
    """Set audit context for the current async task. Returns the generated request_id."""
    request_id = generate_request_id()
    _ctx_request_id.set(request_id)
    _ctx_attempt_no.set(0)
    _ctx_mode.set(mode)
    _ctx_model.set(model)
    _ctx_streaming.set(streaming)
    _ctx_max_retries.set(max_retries)
    _ctx_start_time.set(time.time())
    _ctx_request_payload.set(request_payload)
    _ctx_request_payload_path.set(None)
    _ctx_request_payload_sha256.set(None)
    _ctx_request_payload_size.set(None)
    _ctx_meta.set({})
    return request_id


def update_audit_context(**kwargs: Any) -> None:
    """Merge extra routing/telemetry metadata into the current audit context."""
    current = dict(_ctx_meta.get() or {})
    for key, value in kwargs.items():
        if value is not None:
            current[key] = value
    _ctx_meta.set(current)


def increment_audit_attempt() -> int:
    """Increment and return the current attempt number."""
    current = _ctx_attempt_no.get()
    _ctx_attempt_no.set(current + 1)
    return current + 1


def get_audit_context() -> Dict[str, Any]:
    """Get the current audit context (may be empty if not set)."""
    request_id = _ctx_request_id.get()
    if not request_id:
        return {}
    context = {
        "request_id": request_id,
        "mode": _ctx_mode.get() or "",
        "model_requested": _ctx_model.get(),
        "streaming": _ctx_streaming.get(),
        "attempt_no": _ctx_attempt_no.get(),
        "max_retries": _ctx_max_retries.get(),
        "start_time": _ctx_start_time.get(),
        "request_payload": _ctx_request_payload.get(),
        "request_payload_path": _ctx_request_payload_path.get(),
        "request_payload_sha256": _ctx_request_payload_sha256.get(),
        "request_payload_size": _ctx_request_payload_size.get(),
    }
    context.update(_ctx_meta.get() or {})
    return context


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

async def init_audit_log(db_path: str) -> None:
    """Create the audit table if needed and start the background writer."""
    global _db_path, _initialized, _writer_task, _enabled
    global _raw_enabled, _raw_dir, _raw_retention_days, _raw_max_bytes

    async with _init_lock:
        if _initialized:
            return

        # Check feature flag
        from config import get_config_value
        enabled_raw = os.getenv("AUDIT_LOG_ENABLED")
        if enabled_raw is not None:
            _enabled = enabled_raw.lower() in ("1", "true", "yes", "on")
        else:
            val = await get_config_value("audit_log_enabled", True)
            _enabled = bool(val) if not isinstance(val, str) else val.lower() in ("1", "true", "yes", "on")

        if not _enabled:
            log.info("[AUDIT] Audit log disabled by config")
            _initialized = True
            return

        # Raw payload storage config
        raw_enabled_env = os.getenv("AUDIT_RAW_ENABLED")
        if raw_enabled_env is not None:
            _raw_enabled = raw_enabled_env.lower() in ("1", "true", "yes", "on")
        else:
            raw_enabled_val = await get_config_value("audit_raw_enabled", True)
            _raw_enabled = bool(raw_enabled_val) if not isinstance(raw_enabled_val, str) else raw_enabled_val.lower() in (
                "1",
                "true",
                "yes",
                "on",
            )

        _raw_dir = str(await get_config_value("audit_raw_dir", "./audit_payloads", "AUDIT_RAW_DIR"))
        retention_val = await get_config_value("audit_raw_retention_days", 7, "AUDIT_RAW_RETENTION_DAYS")
        max_bytes_val = await get_config_value("audit_raw_max_bytes", 1024 * 1024, "AUDIT_RAW_MAX_BYTES")
        try:
            _raw_retention_days = max(1, int(retention_val))
        except Exception:
            _raw_retention_days = 7
        try:
            _raw_max_bytes = max(1024, int(max_bytes_val))
        except Exception:
            _raw_max_bytes = 1024 * 1024

        if _raw_enabled:
            try:
                Path(_raw_dir).mkdir(parents=True, exist_ok=True)
                await _prune_old_raw_files()
                log.info(
                    f"[AUDIT] Raw payload capture enabled dir={_raw_dir} retention_days={_raw_retention_days} max_bytes={_raw_max_bytes}"
                )
            except Exception as e:
                log.error(f"[AUDIT] Failed to initialize raw payload directory: {e}")
                _raw_enabled = False

        _db_path = db_path

        try:
            async with aiosqlite.connect(_db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                await _create_audit_table(db)
                await db.commit()
            log.info(f"[AUDIT] Audit log table ready at {_db_path}")
        except Exception as e:
            log.error(f"[AUDIT] Failed to create audit table: {e}")
            _enabled = False
            _initialized = True
            return

        # Start background writer
        _writer_task = asyncio.create_task(_batch_writer(), name="audit_log_writer")
        _initialized = True


async def _create_audit_table(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS request_audit (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,

            -- Request identity
            request_id  TEXT    NOT NULL,
            session_id  TEXT,
            mode        TEXT    NOT NULL,  -- 'antigravity' | 'geminicli'

            -- Credential
            credential_filename TEXT,
            credential_email    TEXT,
            credential_project  TEXT,
            is_ultra            INTEGER DEFAULT 0,

            -- Client
            client_ip_hash      TEXT,
            user_agent_hash     TEXT,

            -- Request details
            model_requested     TEXT,
            model_family        TEXT,
            model_effective     TEXT,
            endpoint_base       TEXT,
            route_provider      TEXT,
            route_policy        TEXT,
            route_reason        TEXT,
            fallback_used       INTEGER DEFAULT 0,
            fallback_detail     TEXT,
            request_payload_path   TEXT,
            request_payload_sha256 TEXT,
            request_payload_size   INTEGER,
            streaming           INTEGER DEFAULT 0,
            attempt_no          INTEGER DEFAULT 0,
            max_retries         INTEGER DEFAULT 0,
            rotated_credential  INTEGER DEFAULT 0,

            -- Response
            http_status         INTEGER,
            latency_ms          REAL,
            tokens_in           INTEGER,
            tokens_out          INTEGER,
            cooldown_until_ts   REAL,
            cooldown_seconds    INTEGER,
            rate_limit_class    TEXT,

            -- Error classification
            error_type          TEXT,
            error_reason        TEXT,
            error_message       TEXT,
            request_pattern     TEXT,
            response_payload_path   TEXT,
            response_payload_sha256 TEXT,
            response_payload_size   INTEGER,

            -- Ban / validation signals
            ban_signal          INTEGER DEFAULT 0,
            validation_required INTEGER DEFAULT 0,

            -- Tool context
            tool_count          INTEGER DEFAULT 0,
            has_web_search      INTEGER DEFAULT 0,

            -- Outcome
            outcome             TEXT   -- 'success'|'retry'|'failed'|'banned'|'validation_blocked'|'no_credential'
        )
    """)

    # Lightweight schema migration for existing DBs
    await _ensure_column(db, "request_audit", "request_payload_path", "TEXT")
    await _ensure_column(db, "request_audit", "request_payload_sha256", "TEXT")
    await _ensure_column(db, "request_audit", "request_payload_size", "INTEGER")
    await _ensure_column(db, "request_audit", "response_payload_path", "TEXT")
    await _ensure_column(db, "request_audit", "response_payload_sha256", "TEXT")
    await _ensure_column(db, "request_audit", "response_payload_size", "INTEGER")
    await _ensure_column(db, "request_audit", "model_family", "TEXT")
    await _ensure_column(db, "request_audit", "cooldown_until_ts", "REAL")
    await _ensure_column(db, "request_audit", "cooldown_seconds", "INTEGER")
    await _ensure_column(db, "request_audit", "rate_limit_class", "TEXT")
    await _ensure_column(db, "request_audit", "route_provider", "TEXT")
    await _ensure_column(db, "request_audit", "route_policy", "TEXT")
    await _ensure_column(db, "request_audit", "route_reason", "TEXT")
    await _ensure_column(db, "request_audit", "fallback_used", "INTEGER DEFAULT 0")
    await _ensure_column(db, "request_audit", "fallback_detail", "TEXT")
    await _ensure_column(db, "request_audit", "request_pattern", "TEXT")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS quota_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ts REAL NOT NULL,
            mode TEXT NOT NULL,
            credential_filename TEXT NOT NULL,
            model_family TEXT NOT NULL,
            window_type TEXT NOT NULL,
            started_at_ts REAL NOT NULL,
            reset_at_ts REAL NOT NULL,
            ended_at_ts REAL,
            state TEXT NOT NULL DEFAULT 'open',
            trigger_request_id TEXT,
            trigger_http_status INTEGER,
            trigger_error_reason TEXT,
            blocked_attempts INTEGER NOT NULL DEFAULT 1,
            last_seen_ts REAL,
            UNIQUE(mode, credential_filename, model_family, window_type, reset_at_ts)
        )
    """)

    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_quota_windows_open
        ON quota_windows(mode, credential_filename, model_family, window_type, state)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_quota_windows_started
        ON quota_windows(started_at_ts)
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS usage_hourly (
            hour_bucket_ts REAL NOT NULL,
            mode TEXT NOT NULL,
            credential_filename TEXT NOT NULL,
            model_family TEXT NOT NULL,
            requests_total INTEGER NOT NULL DEFAULT 0,
            requests_ok INTEGER NOT NULL DEFAULT 0,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hour_bucket_ts, mode, credential_filename, model_family)
        )
    """)

    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_hourly_lookup
        ON usage_hourly(mode, credential_filename, model_family, hour_bucket_ts)
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS usage_hourly_route (
            hour_bucket_ts REAL NOT NULL,
            mode TEXT NOT NULL,
            route_provider TEXT NOT NULL,
            route_policy TEXT NOT NULL,
            requests_total INTEGER NOT NULL DEFAULT 0,
            requests_ok INTEGER NOT NULL DEFAULT 0,
            requests_429 INTEGER NOT NULL DEFAULT 0,
            requests_403 INTEGER NOT NULL DEFAULT 0,
            bans INTEGER NOT NULL DEFAULT 0,
            fallbacks INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hour_bucket_ts, mode, route_provider, route_policy)
        )
    """)

    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_hourly_route_lookup
        ON usage_hourly_route(mode, route_provider, route_policy, hour_bucket_ts)
    """)

    # Indexes for forensic queries
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_ts
        ON request_audit(ts)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_credential
        ON request_audit(credential_filename, ts)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_status
        ON request_audit(http_status, ts)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_ban
        ON request_audit(ban_signal, ts)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_request_id
        ON request_audit(request_id)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_outcome
        ON request_audit(outcome, ts)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_pattern_ts
        ON request_audit(request_pattern, ts)
    """)


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, col_type: str) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    existing = {r[1] for r in rows}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_request_id() -> str:
    """Generate a unique request ID for tracing retries."""
    return f"audit_{uuid.uuid4().hex[:16]}"


def hash_value(value: Optional[str]) -> Optional[str]:
    """One-way hash for PII-safe storage (IP, UA)."""
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _detect_model_family(model_name: Optional[str]) -> str:
    if not model_name:
        return "unknown"
    name = str(model_name).lower()
    if name.startswith("claude-opus"):
        return "claude_ultra"
    if name.startswith("claude-"):
        return "claude_standard"
    if name.startswith("gemini-"):
        return "gemini"
    return "other"


def _classify_rate_limit_class(http_status: Optional[int], cooldown_until_ts: Optional[float], ts: float) -> Optional[str]:
    if http_status != 429 or not cooldown_until_ts:
        return None
    delta = float(cooldown_until_ts) - float(ts)
    if _ROLLING_5H_MIN_SECONDS <= delta <= _ROLLING_5H_MAX_SECONDS:
        return "rolling_5h"
    if delta >= _WEEKLY_LIKE_MIN_SECONDS:
        return "weekly_like"
    if delta <= 10 * 60:
        return "short_rate_limit"
    return "unknown"


def _normalize_payload_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(payload)


def _extract_tool_metrics_from_payload(payload: Any) -> tuple[int, bool]:
    """Best-effort extraction of tool usage hints from request payload."""
    if not isinstance(payload, dict):
        return 0, False

    tools = None
    request = payload.get("request")
    if isinstance(request, dict) and isinstance(request.get("tools"), list):
        tools = request.get("tools")
    elif isinstance(payload.get("tools"), list):
        tools = payload.get("tools")

    if not isinstance(tools, list):
        return 0, False

    has_web_search = False
    for t in tools:
        if not isinstance(t, dict):
            continue
        as_json = ""
        try:
            as_json = json.dumps(t, ensure_ascii=False)
        except Exception:
            as_json = str(t)
        lowered = as_json.lower()
        if "web_search" in lowered or "googlesearch" in lowered or "google_search" in lowered:
            has_web_search = True
            break

    return len(tools), has_web_search


def _payload_has_image(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False

    messages = payload.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and (
                    block.get("type") == "image" or "image_url" in block or "image" in block
                ):
                    return True

    request = payload.get("request")
    if isinstance(request, dict):
        contents = request.get("contents")
        if isinstance(contents, list):
            for c in contents:
                if not isinstance(c, dict):
                    continue
                parts = c.get("parts")
                if not isinstance(parts, list):
                    continue
                for p in parts:
                    if isinstance(p, dict) and ("inlineData" in p or "fileData" in p):
                        return True

    return False


def _extract_prompt_text(payload: Any, limit: int = 16000) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload[:limit]
    if not isinstance(payload, dict):
        return str(payload)[:limit]

    parts: list[str] = []

    messages = payload.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str):
                            parts.append(text)

    request = payload.get("request")
    if isinstance(request, dict):
        contents = request.get("contents")
        if isinstance(contents, list):
            for c in contents:
                if not isinstance(c, dict):
                    continue
                c_parts = c.get("parts")
                if not isinstance(c_parts, list):
                    continue
                for p in c_parts:
                    if isinstance(p, dict):
                        text = p.get("text")
                        if isinstance(text, str):
                            parts.append(text)

    text = "\n".join(parts)
    if not text:
        text = _normalize_payload_text(payload)
    return text[:limit]


def _classify_request_pattern(
    *,
    payload: Any,
    model_requested: Optional[str],
    tool_count: int,
    has_web_search: bool,
    request_payload_size: Optional[int],
) -> str:
    text = _extract_prompt_text(payload)
    lower = text.lower()

    first_line = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped.lower()
            break

    model_lower = str(model_requested or "").lower()

    if _payload_has_image(payload):
        return "image_input"

    if any(h in lower for h in _SECRET_HINTS):
        return "secret_like"

    try:
        import re

        if re.search(r"\b[a-zA-Z0-9_-]{32,}\b", text):
            return "secret_like"
    except Exception:
        pass

    if has_web_search or "-search" in model_lower or "web_search" in lower:
        return "web_search"

    if first_line == "count":
        return "utility_count"

    if "analyze if this message indicates a new conversation topic" in lower:
        return "utility_topic_classifier"

    if "extract any file paths that this command reads or modifies" in lower:
        return "utility_filepath_extractor"

    if "<task-notification>" in lower:
        return "task_notification"

    if "fetch and summarize the content from this url" in lower:
        return "fetch_page_summary"

    if tool_count > 0:
        return "tool_calling"

    try:
        if request_payload_size is not None and int(request_payload_size) >= 100000:
            return "long_context"
    except Exception:
        pass

    return "general_chat"


def _build_raw_relative_path(ts: float, request_id: str, attempt_no: int, kind: str) -> str:
    dt = datetime.fromtimestamp(ts)
    safe_req = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in request_id)
    filename = f"{int(ts)}_{safe_req}_{attempt_no}_{kind}.json"
    return str(Path(str(dt.year), f"{dt.month:02d}", f"{dt.day:02d}", f"{dt.hour:02d}", filename))


async def _write_raw_payload(
    *,
    ts: float,
    request_id: str,
    attempt_no: int,
    kind: str,
    payload: Any,
) -> Dict[str, Any]:
    if not _raw_enabled:
        return {}
    text = _normalize_payload_text(payload)
    if not text:
        return {}

    raw_bytes = text.encode("utf-8", errors="ignore")
    original_size = len(raw_bytes)
    if original_size > _raw_max_bytes:
        raw_bytes = raw_bytes[:_raw_max_bytes]
        truncated = True
    else:
        truncated = False

    sha = hashlib.sha256(raw_bytes).hexdigest()
    rel_path = _build_raw_relative_path(ts, request_id, attempt_no, kind)
    full_path = Path(_raw_dir) / rel_path

    payload_obj: Dict[str, Any] = {
        "request_id": request_id,
        "attempt_no": attempt_no,
        "kind": kind,
        "captured_at": ts,
        "truncated": truncated,
        "original_size": original_size,
        "captured_size": len(raw_bytes),
        "content": raw_bytes.decode("utf-8", errors="ignore"),
    }
    content = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")

    def _sync_write() -> None:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(content)
        try:
            os.chmod(full_path, 0o600)
        except Exception:
            pass

    await asyncio.to_thread(_sync_write)
    return {
        "path": rel_path,
        "sha256": sha,
        "size": original_size,
        "truncated": truncated,
    }


async def _prune_old_raw_files() -> None:
    if not _raw_enabled:
        return
    root = Path(_raw_dir)
    if not root.exists():
        return
    cutoff = time.time() - (_raw_retention_days * 86400)

    def _sync_prune() -> int:
        removed = 0
        for p in root.rglob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                continue
        return removed

    removed_count = await asyncio.to_thread(_sync_prune)
    if removed_count:
        log.info(f"[AUDIT] Pruned {removed_count} raw payload files")


async def read_raw_artifact(path: str) -> Dict[str, Any]:
    """Read a raw artifact by relative path under configured raw dir."""
    if not _raw_enabled:
        return {"error": "raw capture disabled"}
    if not path:
        return {"error": "path required"}

    root = Path(_raw_dir).resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except Exception:
        return {"error": "invalid path"}

    if not target.exists() or not target.is_file():
        return {"error": "artifact not found"}

    try:
        content = await asyncio.to_thread(target.read_text, "utf-8")
        return {"path": path, "content": json.loads(content)}
    except Exception as e:
        return {"error": f"read failed: {e}"}


def classify_error(status_code: Optional[int], error_text: str = "") -> Dict[str, Any]:
    """Classify error into structured fields."""
    result = {
        "error_type": None,
        "error_reason": None,
        "error_message": None,
        "ban_signal": False,
        "validation_required": False,
    }

    if not status_code or status_code == 200:
        return result

    lower = (error_text or "").lower()

    # Ban detection
    for phrase in _BAN_PHRASES:
        if phrase in lower:
            result["ban_signal"] = True
            result["error_type"] = "tos_disabled"
            break

    # Validation detection
    for phrase in _VALIDATION_PHRASES:
        if phrase in lower:
            result["validation_required"] = True
            break

    # Parse JSON error if possible
    try:
        data = json.loads(error_text)
        err = data.get("error", {})
        if isinstance(err, dict):
            result["error_type"] = result["error_type"] or err.get("status", "")
            result["error_reason"] = ""
            details = err.get("details", [])
            for d in details:
                reason = d.get("reason", "")
                if reason:
                    result["error_reason"] = reason
                    break
            msg = err.get("message", "")
            result["error_message"] = str(msg)[:500] if msg else None
    except (json.JSONDecodeError, AttributeError, TypeError):
        if error_text:
            result["error_message"] = error_text[:500]

    # Classify by status code if no type yet
    if not result["error_type"]:
        status_map = {
            429: "rate_limited",
            403: "permission_denied",
            404: "not_found",
            503: "service_unavailable",
            529: "overloaded",
            400: "bad_request",
            401: "unauthorized",
        }
        result["error_type"] = status_map.get(status_code, f"http_{status_code}")

    return result


def determine_outcome(
    status_code: Optional[int],
    error_classification: Dict[str, Any],
    is_retry: bool = False,
    no_credential: bool = False,
) -> str:
    """Determine the outcome category."""
    if no_credential:
        return "no_credential"
    if error_classification.get("ban_signal"):
        return "banned"
    if error_classification.get("validation_required"):
        return "validation_blocked"
    if status_code is None:
        return "failed"
    if status_code == 200:
        return "success"
    if is_retry:
        return "retry"
    return "failed"


async def log_attempt(
    *,
    request_id: str,
    mode: str,
    credential_filename: Optional[str] = None,
    credential_email: Optional[str] = None,
    credential_project: Optional[str] = None,
    is_ultra: bool = False,
    client_ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    model_requested: Optional[str] = None,
    model_effective: Optional[str] = None,
    endpoint_base: Optional[str] = None,
    route_provider: Optional[str] = None,
    route_policy: Optional[str] = None,
    route_reason: Optional[str] = None,
    fallback_used: bool = False,
    fallback_detail: Optional[str] = None,
    streaming: bool = False,
    attempt_no: int = 0,
    max_retries: int = 0,
    rotated_credential: bool = False,
    http_status: Optional[int] = None,
    latency_ms: Optional[float] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cooldown_until_ts: Optional[float] = None,
    error_text: str = "",
    tool_count: Optional[int] = None,
    has_web_search: Optional[bool] = None,
    outcome: Optional[str] = None,
    session_id: Optional[str] = None,
    request_payload: Optional[Any] = None,
    response_payload: Optional[Any] = None,
) -> None:
    """Queue an audit log entry (non-blocking)."""
    if not _enabled:
        return

    error_cls = classify_error(http_status, error_text)

    if outcome is None:
        outcome = determine_outcome(
            http_status, error_cls,
            is_retry=(attempt_no > 0 and http_status != 200),
        )

    ts_now = time.time()

    # Attach raw request payload once per request context.
    request_payload_path = None
    request_payload_sha256 = None
    request_payload_size = None
    response_payload_path = None
    response_payload_sha256 = None
    response_payload_size = None

    request_payload_for_write = request_payload
    if request_payload_for_write is None:
        request_payload_for_write = _ctx_request_payload.get()

    ctx_meta = _ctx_meta.get() or {}
    if model_effective is None:
        model_effective = ctx_meta.get("model_effective")
    if endpoint_base is None:
        endpoint_base = ctx_meta.get("endpoint_base")
    if route_provider is None:
        route_provider = ctx_meta.get("route_provider")
    if route_policy is None:
        route_policy = ctx_meta.get("route_policy")
    if route_reason is None:
        route_reason = ctx_meta.get("route_reason")
    if not fallback_used and ctx_meta.get("fallback_used"):
        fallback_used = True
    if fallback_detail is None:
        fallback_detail = ctx_meta.get("fallback_detail")

    inferred_tool_count, inferred_has_web_search = _extract_tool_metrics_from_payload(
        request_payload_for_write
    )
    if tool_count is None:
        tool_count = inferred_tool_count
    if has_web_search is None:
        has_web_search = inferred_has_web_search

    if _raw_enabled:
        try:
            existing_req_path = _ctx_request_payload_path.get()
            if existing_req_path:
                request_payload_path = existing_req_path
                request_payload_sha256 = _ctx_request_payload_sha256.get()
                request_payload_size = _ctx_request_payload_size.get()
            elif request_payload_for_write is not None:
                req_meta = await _write_raw_payload(
                    ts=ts_now,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    kind="request",
                    payload=request_payload_for_write,
                )
                request_payload_path = req_meta.get("path")
                request_payload_sha256 = req_meta.get("sha256")
                request_payload_size = req_meta.get("size")
                _ctx_request_payload_path.set(request_payload_path)
                _ctx_request_payload_sha256.set(request_payload_sha256)
                _ctx_request_payload_size.set(request_payload_size)
        except Exception as e:
            log.warning(f"[AUDIT] Failed writing raw request payload: {e}")

        # Per-attempt response/error payload capture.
        response_payload_for_write = response_payload
        if response_payload_for_write is None and error_text:
            response_payload_for_write = error_text
        if response_payload_for_write is not None:
            try:
                resp_meta = await _write_raw_payload(
                    ts=ts_now,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    kind="response",
                    payload=response_payload_for_write,
                )
                response_payload_path = resp_meta.get("path")
                response_payload_sha256 = resp_meta.get("sha256")
                response_payload_size = resp_meta.get("size")
            except Exception as e:
                log.warning(f"[AUDIT] Failed writing raw response payload: {e}")

    model_family = _detect_model_family(model_requested or model_effective)
    request_pattern = _classify_request_pattern(
        payload=request_payload_for_write,
        model_requested=model_requested,
        tool_count=int(tool_count or 0),
        has_web_search=bool(has_web_search),
        request_payload_size=request_payload_size,
    )
    rate_limit_class = _classify_rate_limit_class(http_status, cooldown_until_ts, ts_now)
    cooldown_seconds = None
    if cooldown_until_ts is not None:
        try:
            cooldown_seconds = int(max(0, float(cooldown_until_ts) - ts_now))
        except Exception:
            cooldown_seconds = None

    row = {
        "ts": ts_now,
        "request_id": request_id,
        "session_id": session_id,
        "mode": mode,
        "credential_filename": credential_filename,
        "credential_email": credential_email,
        "credential_project": credential_project,
        "is_ultra": 1 if is_ultra else 0,
        "client_ip_hash": hash_value(client_ip),
        "user_agent_hash": hash_value(user_agent),
        "model_requested": model_requested,
        "model_family": model_family,
        "model_effective": model_effective,
        "endpoint_base": endpoint_base,
        "route_provider": route_provider,
        "route_policy": route_policy,
        "route_reason": route_reason,
        "fallback_used": 1 if fallback_used else 0,
        "fallback_detail": fallback_detail,
        "request_payload_path": request_payload_path,
        "request_payload_sha256": request_payload_sha256,
        "request_payload_size": request_payload_size,
        "streaming": 1 if streaming else 0,
        "attempt_no": attempt_no,
        "max_retries": max_retries,
        "rotated_credential": 1 if rotated_credential else 0,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cooldown_until_ts": cooldown_until_ts,
        "cooldown_seconds": cooldown_seconds,
        "rate_limit_class": rate_limit_class,
        "error_type": error_cls.get("error_type"),
        "error_reason": error_cls.get("error_reason"),
        "error_message": error_cls.get("error_message"),
        "request_pattern": request_pattern,
        "response_payload_path": response_payload_path,
        "response_payload_sha256": response_payload_sha256,
        "response_payload_size": response_payload_size,
        "ban_signal": 1 if error_cls.get("ban_signal") else 0,
        "validation_required": 1 if error_cls.get("validation_required") else 0,
        "tool_count": int(tool_count or 0),
        "has_web_search": 1 if has_web_search else 0,
        "outcome": outcome,
    }

    try:
        _write_queue.put_nowait(row)
    except asyncio.QueueFull:
        log.warning("[AUDIT] Write queue full, dropping audit entry")


# ---------------------------------------------------------------------------
# Background batch writer
# ---------------------------------------------------------------------------

async def _batch_writer() -> None:
    """Drain the queue and write rows in batches."""
    while True:
        try:
            batch = []
            # Wait for at least one item
            row = await _write_queue.get()
            batch.append(row)

            # Drain up to 50 more without waiting
            for _ in range(50):
                try:
                    row = _write_queue.get_nowait()
                    batch.append(row)
                except asyncio.QueueEmpty:
                    break

            await _write_batch(batch)

        except asyncio.CancelledError:
            # Flush remaining on shutdown
            remaining = []
            while not _write_queue.empty():
                try:
                    remaining.append(_write_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if remaining:
                await _write_batch(remaining)
            break
        except Exception as e:
            log.error(f"[AUDIT] Batch writer error: {e}")
            await asyncio.sleep(1)


async def _write_batch(batch: list) -> None:
    """Write a batch of rows to SQLite."""
    if not batch or not _db_path:
        return

    columns = [
        "ts", "request_id", "session_id", "mode",
        "credential_filename", "credential_email", "credential_project", "is_ultra",
        "client_ip_hash", "user_agent_hash",
        "model_requested", "model_family", "model_effective", "endpoint_base",
        "route_provider", "route_policy", "route_reason", "fallback_used", "fallback_detail",
        "request_payload_path", "request_payload_sha256", "request_payload_size",
        "streaming", "attempt_no", "max_retries", "rotated_credential",
        "http_status", "latency_ms", "tokens_in", "tokens_out", "cooldown_until_ts", "cooldown_seconds", "rate_limit_class",
        "error_type", "error_reason", "error_message", "request_pattern",
        "response_payload_path", "response_payload_sha256", "response_payload_size",
        "ban_signal", "validation_required",
        "tool_count", "has_web_search",
        "outcome",
    ]

    placeholders = ",".join(["?"] * len(columns))
    col_names = ",".join(columns)
    sql = f"INSERT INTO request_audit ({col_names}) VALUES ({placeholders})"

    try:
        async with aiosqlite.connect(_db_path) as db:
            values = []
            for row in batch:
                values.append(tuple(row.get(c) for c in columns))
            await db.executemany(sql, values)

            for row in batch:
                await _update_usage_hourly(db, row)
                await _update_usage_hourly_route(db, row)
                await _update_quota_windows(db, row)

            await db.commit()
    except Exception as e:
        log.error(f"[AUDIT] Failed to write {len(batch)} audit rows: {e}")


async def _update_quota_windows(db: aiosqlite.Connection, row: Dict[str, Any]) -> None:
    mode = row.get("mode")
    credential_filename = row.get("credential_filename")
    model_family = row.get("model_family") or "unknown"
    ts = float(row.get("ts") or time.time())
    http_status = row.get("http_status")
    rate_limit_class = row.get("rate_limit_class")
    cooldown_until_ts = row.get("cooldown_until_ts")

    if not mode or not credential_filename:
        return

    # Close expired windows lazily whenever we see activity for this key.
    await db.execute(
        """
        UPDATE quota_windows
        SET state='closed', ended_at_ts=COALESCE(ended_at_ts, ?), last_seen_ts=?
        WHERE mode=? AND credential_filename=? AND model_family=?
          AND state='open' AND reset_at_ts <= ?
        """,
        (ts, ts, mode, credential_filename, model_family, ts),
    )

    # Open/increment only for rate-limit windows we care about.
    if http_status != 429 or not cooldown_until_ts:
        return
    if rate_limit_class not in ("rolling_5h", "weekly_like"):
        return

    async with db.execute(
        """
        SELECT id, blocked_attempts
        FROM quota_windows
        WHERE mode=? AND credential_filename=? AND model_family=? AND window_type=?
          AND state='open' AND ABS(reset_at_ts - ?) <= 120
        ORDER BY id DESC
        LIMIT 1
        """,
        (mode, credential_filename, model_family, rate_limit_class, float(cooldown_until_ts)),
    ) as cursor:
        existing = await cursor.fetchone()

    if existing:
        await db.execute(
            """
            UPDATE quota_windows
            SET blocked_attempts=?, last_seen_ts=?
            WHERE id=?
            """,
            (int(existing[1]) + 1, ts, int(existing[0])),
        )
        return

    await db.execute(
        """
        INSERT OR IGNORE INTO quota_windows (
            created_at_ts, mode, credential_filename, model_family, window_type,
            started_at_ts, reset_at_ts, ended_at_ts, state,
            trigger_request_id, trigger_http_status, trigger_error_reason,
            blocked_attempts, last_seen_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'open', ?, ?, ?, 1, ?)
        """,
        (
            ts,
            mode,
            credential_filename,
            model_family,
            rate_limit_class,
            ts,
            float(cooldown_until_ts),
            row.get("request_id"),
            http_status,
            row.get("error_reason") or row.get("error_type"),
            ts,
        ),
    )


def _hour_bucket(ts: float) -> float:
    return float(int(ts // 3600) * 3600)


async def _update_usage_hourly(db: aiosqlite.Connection, row: Dict[str, Any]) -> None:
    mode = row.get("mode")
    credential_filename = row.get("credential_filename")
    if not mode or not credential_filename:
        return

    ts = float(row.get("ts") or time.time())
    bucket = _hour_bucket(ts)
    model_family = row.get("model_family") or _detect_model_family(row.get("model_requested"))
    status = row.get("http_status")

    req_total_inc = 1
    req_ok_inc = 1 if status == 200 else 0

    tokens_in = row.get("tokens_in")
    tokens_out = row.get("tokens_out")
    try:
        tokens_in_inc = int(tokens_in) if tokens_in is not None else 0
    except Exception:
        tokens_in_inc = 0
    try:
        tokens_out_inc = int(tokens_out) if tokens_out is not None else 0
    except Exception:
        tokens_out_inc = 0

    await db.execute(
        """
        INSERT INTO usage_hourly (
            hour_bucket_ts, mode, credential_filename, model_family,
            requests_total, requests_ok, tokens_in, tokens_out
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hour_bucket_ts, mode, credential_filename, model_family)
        DO UPDATE SET
            requests_total = requests_total + excluded.requests_total,
            requests_ok = requests_ok + excluded.requests_ok,
            tokens_in = tokens_in + excluded.tokens_in,
            tokens_out = tokens_out + excluded.tokens_out
        """,
        (
            bucket,
            mode,
            credential_filename,
            model_family,
            req_total_inc,
            req_ok_inc,
            tokens_in_inc,
            tokens_out_inc,
        ),
    )


async def _update_usage_hourly_route(db: aiosqlite.Connection, row: Dict[str, Any]) -> None:
    mode = row.get("mode")
    if not mode:
        return

    ts = float(row.get("ts") or time.time())
    bucket = _hour_bucket(ts)
    route_provider = str(row.get("route_provider") or "unknown")
    route_policy = str(row.get("route_policy") or "unknown")
    status = row.get("http_status")

    req_total_inc = 1
    req_ok_inc = 1 if status == 200 else 0
    req_429_inc = 1 if status == 429 else 0
    req_403_inc = 1 if status == 403 else 0
    bans_inc = 1 if row.get("ban_signal") else 0
    fallback_inc = 1 if row.get("fallback_used") else 0

    await db.execute(
        """
        INSERT INTO usage_hourly_route (
            hour_bucket_ts, mode, route_provider, route_policy,
            requests_total, requests_ok, requests_429, requests_403, bans, fallbacks
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hour_bucket_ts, mode, route_provider, route_policy)
        DO UPDATE SET
            requests_total = requests_total + excluded.requests_total,
            requests_ok = requests_ok + excluded.requests_ok,
            requests_429 = requests_429 + excluded.requests_429,
            requests_403 = requests_403 + excluded.requests_403,
            bans = bans + excluded.bans,
            fallbacks = fallbacks + excluded.fallbacks
        """,
        (
            bucket,
            mode,
            route_provider,
            route_policy,
            req_total_inc,
            req_ok_inc,
            req_429_inc,
            req_403_inc,
            bans_inc,
            fallback_inc,
        ),
    )


# ---------------------------------------------------------------------------
# Query helpers (for /api/audit endpoints)
# ---------------------------------------------------------------------------

async def query_recent(
    limit: int = 100,
    credential: Optional[str] = None,
    status: Optional[int] = None,
    outcome: Optional[str] = None,
    since_hours: float = 24,
) -> list:
    """Query recent audit entries."""
    if not _enabled or not _db_path:
        return []

    conditions = ["ts >= ?"]
    params: list = [time.time() - since_hours * 3600]

    if credential:
        conditions.append("credential_filename = ?")
        params.append(credential)
    if status is not None:
        conditions.append("http_status = ?")
        params.append(status)
    if outcome:
        conditions.append("outcome = ?")
        params.append(outcome)

    where = " AND ".join(conditions)
    sql = f"SELECT * FROM request_audit WHERE {where} ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    try:
        async with aiosqlite.connect(_db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"[AUDIT] Query failed: {e}")
        return []


async def query_incidents(since_hours: float = 72) -> list:
    """Get ban/validation incidents grouped by credential."""
    if not _enabled or not _db_path:
        return []

    since = time.time() - since_hours * 3600
    sql = """
        SELECT
            credential_filename,
            credential_email,
            mode,
            COUNT(*) as total_attempts,
            SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) as successes,
            SUM(CASE WHEN ban_signal = 1 THEN 1 ELSE 0 END) as ban_hits,
            SUM(CASE WHEN validation_required = 1 THEN 1 ELSE 0 END) as validation_hits,
            SUM(CASE WHEN http_status = 429 THEN 1 ELSE 0 END) as rate_limits,
            SUM(CASE WHEN http_status = 503 THEN 1 ELSE 0 END) as capacity_errors,
            SUM(CASE WHEN http_status = 403 THEN 1 ELSE 0 END) as permission_errors,
            SUM(CASE WHEN http_status = 404 THEN 1 ELSE 0 END) as not_found_errors,
            MIN(ts) as first_seen,
            MAX(ts) as last_seen,
            GROUP_CONCAT(DISTINCT model_requested) as models_used
        FROM request_audit
        WHERE ts >= ?
        GROUP BY credential_filename
        ORDER BY ban_hits DESC, permission_errors DESC, total_attempts DESC
    """

    try:
        async with aiosqlite.connect(_db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (since,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"[AUDIT] Incidents query failed: {e}")
        return []


async def query_credential_timeline(
    credential_filename: str,
    since_hours: float = 48,
) -> list:
    """Get chronological event history for a single credential."""
    if not _enabled or not _db_path:
        return []

    since = time.time() - since_hours * 3600
    sql = """
        SELECT ts, request_id, model_requested, http_status, outcome,
               error_type, error_reason, ban_signal, validation_required,
               attempt_no, rotated_credential, latency_ms,
               request_payload_path, response_payload_path
        FROM request_audit
        WHERE credential_filename = ? AND ts >= ?
        ORDER BY ts ASC
    """

    try:
        async with aiosqlite.connect(_db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (credential_filename, since)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"[AUDIT] Timeline query failed: {e}")
        return []


async def get_stats(since_hours: float = 24) -> dict:
    """Get aggregate stats for dashboard."""
    if not _enabled or not _db_path:
        return {"enabled": False}

    since = time.time() - since_hours * 3600
    sql = """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) as successes,
            SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) as failures,
            SUM(CASE WHEN outcome = 'retry' THEN 1 ELSE 0 END) as retries,
            SUM(CASE WHEN outcome = 'banned' THEN 1 ELSE 0 END) as bans,
            SUM(CASE WHEN outcome = 'validation_blocked' THEN 1 ELSE 0 END) as validations,
            SUM(CASE WHEN outcome = 'no_credential' THEN 1 ELSE 0 END) as no_cred,
            COUNT(DISTINCT credential_filename) as credentials_used,
            AVG(latency_ms) as avg_latency_ms
        FROM request_audit
        WHERE ts >= ?
    """

    try:
        async with aiosqlite.connect(_db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (since,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {"enabled": True, "since_hours": since_hours, **dict(row)}
                return {"enabled": True, "total": 0}
    except Exception as e:
        log.error(f"[AUDIT] Stats query failed: {e}")
        return {"enabled": True, "error": str(e)}


async def query_quota_windows(
    since_days: float = 30,
    window_type: Optional[str] = None,
    mode: Optional[str] = None,
    credential: Optional[str] = None,
    model_family: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 200,
) -> list:
    if not _enabled or not _db_path:
        return []

    conditions = ["started_at_ts >= ?"]
    params: list[Any] = [time.time() - since_days * 86400]
    if window_type:
        conditions.append("window_type = ?")
        params.append(window_type)
    if mode:
        conditions.append("mode = ?")
        params.append(mode)
    if credential:
        conditions.append("credential_filename = ?")
        params.append(credential)
    if model_family:
        conditions.append("model_family = ?")
        params.append(model_family)
    if state:
        conditions.append("state = ?")
        params.append(state)

    sql = f"""
        SELECT *
        FROM quota_windows
        WHERE {' AND '.join(conditions)}
        ORDER BY started_at_ts DESC
        LIMIT ?
    """
    params.append(limit)

    try:
        async with aiosqlite.connect(_db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"[AUDIT] Quota windows query failed: {e}")
        return []


async def query_quota_weekly_hits(
    weeks: int = 12,
    mode: Optional[str] = None,
    model_family: Optional[str] = None,
) -> list:
    if not _enabled or not _db_path:
        return []

    since = time.time() - max(1, weeks) * 7 * 86400
    conditions = ["started_at_ts >= ?"]
    params: list[Any] = [since]
    if mode:
        conditions.append("mode = ?")
        params.append(mode)
    if model_family:
        conditions.append("model_family = ?")
        params.append(model_family)

    sql = f"""
        SELECT
            strftime('%Y-W%W', datetime(started_at_ts, 'unixepoch')) AS week_utc,
            mode,
            model_family,
            credential_filename,
            SUM(CASE WHEN window_type = 'rolling_5h' THEN 1 ELSE 0 END) AS rolling_5h_hits,
            SUM(CASE WHEN window_type = 'weekly_like' THEN 1 ELSE 0 END) AS weekly_like_hits,
            COUNT(*) AS total_hits
        FROM quota_windows
        WHERE {' AND '.join(conditions)}
        GROUP BY week_utc, mode, model_family, credential_filename
        ORDER BY week_utc DESC, total_hits DESC
    """

    try:
        async with aiosqlite.connect(_db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"[AUDIT] Weekly quota hits query failed: {e}")
        return []


async def query_quota_prehit_usage(
    since_days: float = 30,
    lookback_hours: float = 5,
    window_type: str = "rolling_5h",
    mode: Optional[str] = None,
    credential: Optional[str] = None,
    model_family: Optional[str] = None,
    limit: int = 200,
) -> list:
    """For each quota window hit, return usage aggregated before the hit."""
    if not _enabled or not _db_path:
        return []

    since = time.time() - max(1.0, since_days) * 86400
    lb_seconds = max(1.0, lookback_hours) * 3600

    conditions = ["qw.started_at_ts >= ?", "qw.window_type = ?"]
    params: list[Any] = [since, window_type]
    if mode:
        conditions.append("qw.mode = ?")
        params.append(mode)
    if credential:
        conditions.append("qw.credential_filename = ?")
        params.append(credential)
    if model_family:
        conditions.append("qw.model_family = ?")
        params.append(model_family)

    sql = f"""
        SELECT
            qw.id,
            qw.mode,
            qw.credential_filename,
            qw.model_family,
            qw.window_type,
            qw.started_at_ts,
            qw.reset_at_ts,
            qw.blocked_attempts,
            qw.trigger_request_id,
            qw.trigger_error_reason,
            COALESCE(SUM(uh.requests_total), 0) AS prehit_requests_total,
            COALESCE(SUM(uh.requests_ok), 0) AS prehit_requests_ok,
            COALESCE(SUM(uh.tokens_in), 0) AS prehit_tokens_in,
            COALESCE(SUM(uh.tokens_out), 0) AS prehit_tokens_out
        FROM quota_windows qw
        LEFT JOIN usage_hourly uh
          ON uh.mode = qw.mode
         AND uh.credential_filename = qw.credential_filename
         AND uh.model_family = qw.model_family
         AND uh.hour_bucket_ts >= (qw.started_at_ts - ?)
         AND uh.hour_bucket_ts < qw.started_at_ts
        WHERE {' AND '.join(conditions)}
        GROUP BY
            qw.id,
            qw.mode,
            qw.credential_filename,
            qw.model_family,
            qw.window_type,
            qw.started_at_ts,
            qw.reset_at_ts,
            qw.blocked_attempts,
            qw.trigger_request_id,
            qw.trigger_error_reason
        ORDER BY qw.started_at_ts DESC
        LIMIT ?
    """

    query_params = [lb_seconds, *params, limit]

    try:
        async with aiosqlite.connect(_db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, query_params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"[AUDIT] Pre-hit quota usage query failed: {e}")
        return []
