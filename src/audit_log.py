"""
Audit log for API request attempts.

Append-only table that records every upstream API attempt with structured fields
for forensic analysis, ban detection, and incident postmortem.

Feature-gated via AUDIT_LOG_ENABLED config (default: True).
"""

import asyncio
import contextvars
import hashlib
import json
import os
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

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_db_path: Optional[str] = None
_enabled: bool = True
_initialized: bool = False
_init_lock = asyncio.Lock()

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


def set_audit_context(
    mode: str,
    model: Optional[str] = None,
    streaming: bool = False,
    max_retries: int = 0,
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
    return request_id


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
    return {
        "request_id": request_id,
        "mode": _ctx_mode.get() or "",
        "model_requested": _ctx_model.get(),
        "streaming": _ctx_streaming.get(),
        "attempt_no": _ctx_attempt_no.get(),
        "max_retries": _ctx_max_retries.get(),
        "start_time": _ctx_start_time.get(),
    }


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

async def init_audit_log(db_path: str) -> None:
    """Create the audit table if needed and start the background writer."""
    global _db_path, _initialized, _writer_task, _enabled

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
            model_effective     TEXT,
            endpoint_base       TEXT,
            streaming           INTEGER DEFAULT 0,
            attempt_no          INTEGER DEFAULT 0,
            max_retries         INTEGER DEFAULT 0,
            rotated_credential  INTEGER DEFAULT 0,

            -- Response
            http_status         INTEGER,
            latency_ms          REAL,
            tokens_in           INTEGER,
            tokens_out          INTEGER,

            -- Error classification
            error_type          TEXT,
            error_reason        TEXT,
            error_message       TEXT,

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
    streaming: bool = False,
    attempt_no: int = 0,
    max_retries: int = 0,
    rotated_credential: bool = False,
    http_status: Optional[int] = None,
    latency_ms: Optional[float] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    error_text: str = "",
    tool_count: int = 0,
    has_web_search: bool = False,
    outcome: Optional[str] = None,
    session_id: Optional[str] = None,
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

    row = {
        "ts": time.time(),
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
        "model_effective": model_effective,
        "endpoint_base": endpoint_base,
        "streaming": 1 if streaming else 0,
        "attempt_no": attempt_no,
        "max_retries": max_retries,
        "rotated_credential": 1 if rotated_credential else 0,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "error_type": error_cls.get("error_type"),
        "error_reason": error_cls.get("error_reason"),
        "error_message": error_cls.get("error_message"),
        "ban_signal": 1 if error_cls.get("ban_signal") else 0,
        "validation_required": 1 if error_cls.get("validation_required") else 0,
        "tool_count": tool_count,
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
        "model_requested", "model_effective", "endpoint_base",
        "streaming", "attempt_no", "max_retries", "rotated_credential",
        "http_status", "latency_ms", "tokens_in", "tokens_out",
        "error_type", "error_reason", "error_message",
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
            await db.commit()
    except Exception as e:
        log.error(f"[AUDIT] Failed to write {len(batch)} audit rows: {e}")


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
               attempt_no, rotated_credential, latency_ms
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
