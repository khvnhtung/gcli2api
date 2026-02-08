"""
Retry Strategy Module - Enhanced with Smart Backoff

Provides intelligent retry with exponential backoff for different error types.
Enhanced with error classification to distinguish between:
- QUOTA_EXHAUSTED: Daily/hourly quota used up (rotate account, long wait)
- MODEL_CAPACITY_EXHAUSTED: Google infrastructure overloaded (rotate account, short wait)
- RATE_LIMIT_EXCEEDED: Per-minute rate limit (standard backoff)
- SERVER_ERROR: 5xx errors (exponential backoff)

Ported from antigravity-claude-proxy for improved error handling.
"""

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple

from log import log


# ============================================================================
# Enums
# ============================================================================


class RetryStrategy(Enum):
    """Retry strategy types."""
    NO_RETRY = "no_retry"
    FIXED_DELAY = "fixed_delay"
    LINEAR_BACKOFF = "linear_backoff"
    EXPONENTIAL_BACKOFF = "exponential_backoff"


class RateLimitReason(Enum):
    """Classification of rate limit/error reasons."""
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"           # Daily/hourly quota used up
    MODEL_CAPACITY_EXHAUSTED = "MODEL_CAPACITY_EXHAUSTED"  # Google infrastructure overloaded
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"   # Per-minute rate limit
    SERVER_ERROR = "SERVER_ERROR"                 # 5xx errors
    AUTH_ERROR = "AUTH_ERROR"                     # 401/403 errors
    UNKNOWN = "UNKNOWN"                           # Unclassified


# ============================================================================
# Smart Backoff Constants (ported from antigravity-claude-proxy)
# ============================================================================

# Backoff delays by error type (milliseconds)
BACKOFF_BY_ERROR_TYPE: Dict[RateLimitReason, int] = {
    RateLimitReason.RATE_LIMIT_EXCEEDED: 30000,       # 30 seconds
    RateLimitReason.MODEL_CAPACITY_EXHAUSTED: 15000,  # 15 seconds
    RateLimitReason.SERVER_ERROR: 20000,              # 20 seconds
    RateLimitReason.UNKNOWN: 60000,                   # 1 minute
}

# Progressive backoff tiers for QUOTA_EXHAUSTED [60s, 5m, 30m, 2h]
QUOTA_EXHAUSTED_BACKOFF_TIERS_MS = [60000, 300000, 1800000, 7200000]

# Capacity exhaustion backoff tiers [5s, 10s, 20s, 30s, 60s]
CAPACITY_BACKOFF_TIERS_MS = [5000, 10000, 20000, 30000, 60000]

# Minimum backoff floor to prevent "Available in 0s" loops
MIN_BACKOFF_MS = 2000

# Rate limit deduplication window (prevents thundering herd)
RATE_LIMIT_DEDUP_WINDOW_MS = 2000  # 2 seconds

# Reset consecutive failure counter after this inactivity period
RATE_LIMIT_STATE_RESET_MS = 120000  # 2 minutes

# First retry delay (quick retry on first 429)
FIRST_RETRY_DELAY_MS = 1000  # 1 second


# ============================================================================
# Duration Parsing
# ============================================================================


def parse_duration_ms(duration_str: str) -> Optional[int]:
    """
    Parse duration string like '42s', '1.5s', '200ms', '1h16m0.667s' to milliseconds.

    Args:
        duration_str: Duration string from Google's RetryInfo

    Returns:
        Duration in milliseconds, or None if parsing failed
    """
    pattern = r"([\d.]+)\s*(ms|s|m|h)"
    total_ms = 0.0
    matched = False

    for match in re.finditer(pattern, duration_str):
        matched = True
        value = float(match.group(1))
        unit = match.group(2)

        if unit == "ms":
            total_ms += value
        elif unit == "s":
            total_ms += value * 1000
        elif unit == "m":
            total_ms += value * 60 * 1000
        elif unit == "h":
            total_ms += value * 60 * 60 * 1000

    return int(total_ms) if matched else None


def parse_retry_delay_from_error(error_text: str) -> Optional[int]:
    """
    Extract retry delay from Google's error response.

    Parses the RetryInfo.retryDelay or metadata.quotaResetDelay from
    Google's error response JSON.

    Args:
        error_text: Raw error response text (JSON)

    Returns:
        Retry delay in milliseconds, or None if not found
    """
    try:
        data = json.loads(error_text)
        details = data.get("error", {}).get("details", [])

        for detail in details:
            # Method 1: RetryInfo.retryDelay
            type_str = detail.get("@type", "")
            if "RetryInfo" in type_str:
                retry_delay = detail.get("retryDelay")
                if retry_delay:
                    return parse_duration_ms(retry_delay)

            # Method 2: metadata.quotaResetDelay
            quota_delay = detail.get("metadata", {}).get("quotaResetDelay")
            if quota_delay:
                return parse_duration_ms(quota_delay)
    except Exception:
        pass

    return None


# ============================================================================
# Error Classification
# ============================================================================


def parse_rate_limit_reason(error_text: str) -> RateLimitReason:
    """
    Parse the rate limit reason from error text.

    Classifies errors into:
    - QUOTA_EXHAUSTED: Daily/hourly limits (rotate account, long wait)
    - MODEL_CAPACITY_EXHAUSTED: Google overloaded (same account, short wait)
    - RATE_LIMIT_EXCEEDED: Per-minute limits (standard backoff)
    - SERVER_ERROR: 5xx errors (exponential backoff)
    - UNKNOWN: Unclassified

    Args:
        error_text: Error message/body text

    Returns:
        RateLimitReason enum value
    """
    lower = (error_text or "").lower()

    # Check for quota exhaustion (daily/hourly limits)
    if any(x in lower for x in [
        "quota_exhausted",
        "quotaresetdelay",
        "quotaresettimestamp",
        "resource_exhausted",
        "daily limit",
        "quota exceeded",
    ]):
        return RateLimitReason.QUOTA_EXHAUSTED

    # Check for model capacity issues (temporary, retry quickly on SAME account)
    if any(x in lower for x in [
        "model_capacity_exhausted",
        "capacity_exhausted",
        "model is currently overloaded",
        "service temporarily unavailable",
    ]):
        return RateLimitReason.MODEL_CAPACITY_EXHAUSTED

    # Check for rate limiting (per-minute limits)
    if any(x in lower for x in [
        "rate_limit_exceeded",
        "rate limit",
        "too many requests",
        "throttl",
    ]):
        return RateLimitReason.RATE_LIMIT_EXCEEDED

    # Check for server errors
    if any(x in lower for x in [
        "internal server error",
        "server error",
        "503",
        "502",
        "504",
    ]):
        return RateLimitReason.SERVER_ERROR

    return RateLimitReason.UNKNOWN


def is_model_capacity_exhausted(error_text: str) -> bool:
    """
    Check if error is due to model capacity (not user quota).

    Args:
        error_text: Error message

    Returns:
        True if capacity exhausted (not quota)
    """
    return parse_rate_limit_reason(error_text) == RateLimitReason.MODEL_CAPACITY_EXHAUSTED


# ============================================================================
# Rate Limit State Tracking
# ============================================================================


@dataclass
class RateLimitState:
    """Tracks rate limit state for a credential+model combination."""
    consecutive_failures: int = 0
    last_failure_at: float = 0.0  # timestamp
    last_reason: Optional[RateLimitReason] = None


class RateLimitTracker:
    """
    Tracks rate limit state per credential+model to enable:
    1. Deduplication (prevent thundering herd on concurrent 429s)
    2. Progressive backoff (escalate delays on consecutive failures)
    3. State cleanup (reset after inactivity)

    Thread-safe implementation.
    """

    def __init__(self):
        self._state: Dict[str, RateLimitState] = {}
        self._lock = threading.Lock()

    def _get_key(self, credential_id: str, model: str) -> str:
        """Generate deduplication key."""
        return f"{credential_id}:{model}"

    def get_backoff_info(
        self,
        credential_id: str,
        model: str,
        error_text: str,
        server_retry_ms: Optional[int] = None,
    ) -> Tuple[int, int, bool, RateLimitReason]:
        """
        Get backoff information with deduplication and progressive escalation.

        Args:
            credential_id: Credential identifier (filename or email)
            model: Model name
            error_text: Error message for classification
            server_retry_ms: Server-provided retry delay (if any)

        Returns:
            Tuple of (delay_ms, attempt_number, is_duplicate, reason)
        """
        now = time.time()
        key = self._get_key(credential_id, model)
        reason = parse_rate_limit_reason(error_text)

        with self._lock:
            state = self._state.get(key)

            # Check if within dedup window
            if state and (now - state.last_failure_at) < (RATE_LIMIT_DEDUP_WINDOW_MS / 1000):
                # Duplicate request - return cached backoff
                delay_ms = self._calculate_delay(
                    reason, state.consecutive_failures, server_retry_ms
                )
                log.debug(
                    f"[SmartBackoff] Duplicate 429 for {credential_id}:{model}, "
                    f"attempt={state.consecutive_failures}, delay={delay_ms}ms"
                )
                return (delay_ms, state.consecutive_failures, True, reason)

            # Determine attempt number - reset after inactivity
            if state and (now - state.last_failure_at) < (RATE_LIMIT_STATE_RESET_MS / 1000):
                attempt = state.consecutive_failures + 1
            else:
                attempt = 1

            # Update state
            self._state[key] = RateLimitState(
                consecutive_failures=attempt,
                last_failure_at=now,
                last_reason=reason,
            )

            delay_ms = self._calculate_delay(reason, attempt, server_retry_ms)

            log.debug(
                f"[SmartBackoff] {credential_id}:{model} - "
                f"reason={reason.value}, attempt={attempt}, delay={delay_ms}ms"
            )

            return (delay_ms, attempt, False, reason)

    def _calculate_delay(
        self,
        reason: RateLimitReason,
        attempt: int,
        server_retry_ms: Optional[int],
    ) -> int:
        """Calculate delay based on reason and attempt number."""

        # If server provides a reset time, use it (with minimum floor)
        if server_retry_ms and server_retry_ms > 0:
            return max(server_retry_ms, MIN_BACKOFF_MS)

        if reason == RateLimitReason.QUOTA_EXHAUSTED:
            # Progressive backoff: [60s, 5m, 30m, 2h]
            tier_index = min(attempt - 1, len(QUOTA_EXHAUSTED_BACKOFF_TIERS_MS) - 1)
            return QUOTA_EXHAUSTED_BACKOFF_TIERS_MS[tier_index]

        elif reason == RateLimitReason.MODEL_CAPACITY_EXHAUSTED:
            # Progressive but shorter: [5s, 10s, 20s, 30s, 60s]
            tier_index = min(attempt - 1, len(CAPACITY_BACKOFF_TIERS_MS) - 1)
            return CAPACITY_BACKOFF_TIERS_MS[tier_index]

        elif reason in BACKOFF_BY_ERROR_TYPE:
            return BACKOFF_BY_ERROR_TYPE[reason]

        else:
            return BACKOFF_BY_ERROR_TYPE[RateLimitReason.UNKNOWN]

    def clear_state(self, credential_id: str, model: str) -> None:
        """Clear state after successful request."""
        key = self._get_key(credential_id, model)
        with self._lock:
            self._state.pop(key, None)

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count of removed entries."""
        now = time.time()
        cutoff = now - (RATE_LIMIT_STATE_RESET_MS / 1000)
        removed = 0

        with self._lock:
            expired_keys = [
                k for k, v in self._state.items()
                if v.last_failure_at < cutoff
            ]
            for k in expired_keys:
                del self._state[k]
                removed += 1

        if removed > 0:
            log.debug(f"[SmartBackoff] Cleaned up {removed} expired rate limit entries")

        return removed

    def get_state_count(self) -> int:
        """Get number of tracked states (for debugging)."""
        with self._lock:
            return len(self._state)


# Global tracker instance
_rate_limit_tracker = RateLimitTracker()


def get_rate_limit_tracker() -> RateLimitTracker:
    """Get the global rate limit tracker instance."""
    return _rate_limit_tracker


# ============================================================================
# Retry Strategy Determination
# ============================================================================


def determine_retry_strategy(
    status_code: int,
    error_text: str = "",
    credential_id: str = "",
    model: str = "",
) -> Tuple[RetryStrategy, int, int, RateLimitReason]:
    """
    Determine retry strategy based on error status code and error text.

    Enhanced with smart backoff by error type:
    - QUOTA_EXHAUSTED: Progressive [60s, 5m, 30m, 2h], rotate account
    - MODEL_CAPACITY_EXHAUSTED: Short delays [2s-15s], rotate account
    - RATE_LIMIT_EXCEEDED: Standard 30s backoff
    - SERVER_ERROR: Exponential backoff

    Args:
        status_code: HTTP status code
        error_text: Raw error response text
        credential_id: Credential identifier for state tracking (optional)
        model: Model name for state tracking (optional)

    Returns:
        Tuple of (strategy, base_delay_ms, max_delay_ms, reason)
    """
    reason = RateLimitReason.UNKNOWN

    if status_code == 429:
        # Use smart backoff with state tracking
        tracker = get_rate_limit_tracker()

        # Try to parse server's suggested delay
        server_delay_ms = parse_retry_delay_from_error(error_text)

        if credential_id and model:
            delay_ms, attempt, is_dup, reason = tracker.get_backoff_info(
                credential_id, model, error_text, server_delay_ms
            )
            return (RetryStrategy.FIXED_DELAY, delay_ms, delay_ms, reason)
        else:
            # Fallback without state tracking
            reason = parse_rate_limit_reason(error_text)
            if server_delay_ms:
                delay_ms = max(server_delay_ms + 200, MIN_BACKOFF_MS)
                return (RetryStrategy.FIXED_DELAY, delay_ms, delay_ms, reason)
            else:
                return (RetryStrategy.LINEAR_BACKOFF, 5000, 30000, reason)

    elif status_code in (503, 529):
        reason = RateLimitReason.MODEL_CAPACITY_EXHAUSTED
        # Short delay + rotate: spread load across accounts under capacity pressure
        return (RetryStrategy.LINEAR_BACKOFF, 2000, 15000, reason)

    elif status_code == 500:
        reason = RateLimitReason.SERVER_ERROR
        return (RetryStrategy.LINEAR_BACKOFF, 3000, 30000, reason)

    elif status_code in (401, 403):
        reason = RateLimitReason.AUTH_ERROR
        return (RetryStrategy.FIXED_DELAY, 200, 200, reason)

    else:
        return (RetryStrategy.NO_RETRY, 0, 0, reason)


def should_rotate_account(status_code: int, error_text: str = "") -> bool:
    """
    Determine if we should try a different account.

    Enhanced logic:
    - QUOTA_EXHAUSTED (429): Rotate - this account's quota is used
    - MODEL_CAPACITY_EXHAUSTED (429): DON'T rotate - Google's issue, not account
    - RATE_LIMIT_EXCEEDED (429): Rotate - per-account limit
    - 401/403: Rotate - account-level auth issue
    - 500: Rotate - might be account-specific
    - 503/529: Rotate - spread load across accounts under capacity pressure

    Args:
        status_code: HTTP status code
        error_text: Error message for classification

    Returns:
        True if should rotate to a different account
    """
    if status_code == 429:
        reason = parse_rate_limit_reason(error_text)

        # DON'T rotate for capacity exhaustion - it's Google's infrastructure
        if reason == RateLimitReason.MODEL_CAPACITY_EXHAUSTED:
            log.debug(f"[SmartBackoff] Not rotating for MODEL_CAPACITY_EXHAUSTED")
            return False

        # Rotate for quota/rate limit issues
        return True

    # Auth errors - rotate
    if status_code in (401, 403):
        return True

    # Server errors - might be account-specific, try rotating
    if status_code == 500:
        return True

    # Capacity errors (503, 529) - rotate to spread load across accounts
    if status_code in (503, 529):
        return True

    return False


# ============================================================================
# Delay Calculation and Application
# ============================================================================


def calculate_delay_ms(
    strategy: RetryStrategy,
    base_ms: int,
    max_ms: int,
    attempt: int
) -> int:
    """
    Calculate the actual delay based on strategy and attempt number.

    Args:
        strategy: The retry strategy to use
        base_ms: Base delay in milliseconds
        max_ms: Maximum delay in milliseconds
        attempt: Current attempt number (0-based)

    Returns:
        Delay in milliseconds
    """
    if strategy == RetryStrategy.NO_RETRY:
        return 0
    elif strategy == RetryStrategy.FIXED_DELAY:
        return base_ms
    elif strategy == RetryStrategy.LINEAR_BACKOFF:
        return min(base_ms * (attempt + 1), max_ms)
    elif strategy == RetryStrategy.EXPONENTIAL_BACKOFF:
        return min(base_ms * (2 ** attempt), max_ms)
    else:
        return 0


async def apply_retry_delay(
    strategy: RetryStrategy,
    base_ms: int,
    max_ms: int,
    attempt: int,
    trace_id: str = "",
    reason: Optional[RateLimitReason] = None,
) -> bool:
    """
    Apply retry delay based on strategy.

    Args:
        strategy: The retry strategy to use
        base_ms: Base delay in milliseconds
        max_ms: Maximum delay in milliseconds
        attempt: Current attempt number (0-based)
        trace_id: Optional trace ID for logging
        reason: Optional rate limit reason for logging

    Returns:
        True if should continue retrying, False otherwise
    """
    if strategy == RetryStrategy.NO_RETRY:
        log.debug(f"[{trace_id}] Non-retryable error, stopping")
        return False

    delay_ms = calculate_delay_ms(strategy, base_ms, max_ms, attempt)

    reason_str = f", reason={reason.value}" if reason else ""
    log.info(
        f"[{trace_id}] ⏱️ Retry with {strategy.value}: "
        f"attempt={attempt + 1}, delay={delay_ms}ms{reason_str}"
    )

    await asyncio.sleep(delay_ms / 1000.0)
    return True


def is_retryable_error(status_code: int, error_text: str = "") -> bool:
    """
    Check if an error status code is retryable.

    Args:
        status_code: HTTP status code
        error_text: Error text for classification

    Returns:
        True if the error should be retried
    """
    strategy, _, _, _ = determine_retry_strategy(status_code, error_text)
    return strategy != RetryStrategy.NO_RETRY


# ============================================================================
# Background Cleanup
# ============================================================================

_cleanup_task: Optional[asyncio.Task] = None


async def _periodic_cleanup():
    """Background task to clean up expired rate limit state."""
    while True:
        await asyncio.sleep(60)  # Every 60 seconds
        try:
            tracker = get_rate_limit_tracker()
            tracker.cleanup_expired()
        except Exception as e:
            log.debug(f"[SmartBackoff] Cleanup error: {e}")


def start_cleanup_task():
    """Start the background cleanup task (call once at startup)."""
    global _cleanup_task
    try:
        loop = asyncio.get_running_loop()
        if _cleanup_task is None or _cleanup_task.done():
            _cleanup_task = loop.create_task(_periodic_cleanup())
            log.debug("[SmartBackoff] Started background cleanup task")
    except RuntimeError:
        # No running loop - will be started later
        pass


def stop_cleanup_task():
    """Stop the background cleanup task."""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
        log.debug("[SmartBackoff] Stopped background cleanup task")


# ============================================================================
# Backward Compatibility
# ============================================================================

# For code that calls determine_retry_strategy with old signature
def determine_retry_strategy_legacy(
    status_code: int,
    error_text: str = ""
) -> Tuple[RetryStrategy, int, int]:
    """
    Legacy version of determine_retry_strategy for backward compatibility.

    Returns:
        Tuple of (strategy, base_delay_ms, max_delay_ms)
    """
    strategy, base_ms, max_ms, _ = determine_retry_strategy(status_code, error_text)
    return (strategy, base_ms, max_ms)


# For code that calls should_rotate_account with old signature
def should_rotate_account_legacy(status_code: int) -> bool:
    """
    Legacy version of should_rotate_account for backward compatibility.
    """
    return status_code in (429, 401, 403, 500)
