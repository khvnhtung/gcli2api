"""
Retry Strategy Module - Ported from Antigravity-Manager

Provides intelligent retry with exponential backoff for different error types.
This module handles 429, 503, 529, 500 errors with appropriate backoff strategies.
"""

import asyncio
import json
import re
from enum import Enum
from typing import Optional, Tuple

from log import log


class RetryStrategy(Enum):
    """Retry strategy types."""
    NO_RETRY = "no_retry"
    FIXED_DELAY = "fixed_delay"
    LINEAR_BACKOFF = "linear_backoff"
    EXPONENTIAL_BACKOFF = "exponential_backoff"


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


def determine_retry_strategy(
    status_code: int,
    error_text: str = ""
) -> Tuple[RetryStrategy, int, int]:
    """
    Determine retry strategy based on error status code.

    Strategy selection (ported from Antigravity-Manager):
    - 429: Parse server's RetryInfo, else LinearBackoff 5s
    - 503/529: ExponentialBackoff 10s -> 60s (capacity exhausted)
    - 500: LinearBackoff 3s (server internal error)
    - 401/403: FixedDelay 200ms (quick account switch)
    - Others: NoRetry

    Args:
        status_code: HTTP status code
        error_text: Raw error response text

    Returns:
        Tuple of (strategy, base_delay_ms, max_delay_ms)
    """
    if status_code == 429:
        # Try to parse server's suggested delay
        delay_ms = parse_retry_delay_from_error(error_text)
        if delay_ms:
            # Add 200ms buffer, cap at 30s
            actual_delay = min(delay_ms + 200, 30000)
            return (RetryStrategy.FIXED_DELAY, actual_delay, actual_delay)
        else:
            # Linear backoff: 5s * (attempt + 1)
            return (RetryStrategy.LINEAR_BACKOFF, 5000, 30000)

    elif status_code in (503, 529):
        # Exponential backoff for capacity exhausted
        # 10s -> 20s -> 40s -> 60s (capped)
        return (RetryStrategy.EXPONENTIAL_BACKOFF, 10000, 60000)

    elif status_code == 500:
        # Linear backoff for server internal error
        return (RetryStrategy.LINEAR_BACKOFF, 3000, 30000)

    elif status_code in (401, 403):
        # Quick delay before account rotation
        return (RetryStrategy.FIXED_DELAY, 200, 200)

    else:
        # 400 and other errors: don't retry
        return (RetryStrategy.NO_RETRY, 0, 0)


def should_rotate_account(status_code: int) -> bool:
    """
    Determine if we should try a different account.

    Account rotation logic (from Antigravity-Manager):
    - 429, 401, 403, 500: Account-level issues, rotate
    - 400, 503, 529: Global/protocol issues, rotating won't help

    Args:
        status_code: HTTP status code

    Returns:
        True if should rotate to a different account
    """
    return status_code in (429, 401, 403, 500)


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
    trace_id: str = ""
) -> bool:
    """
    Apply retry delay based on strategy.

    Args:
        strategy: The retry strategy to use
        base_ms: Base delay in milliseconds
        max_ms: Maximum delay in milliseconds
        attempt: Current attempt number (0-based)
        trace_id: Optional trace ID for logging

    Returns:
        True if should continue retrying, False otherwise
    """
    if strategy == RetryStrategy.NO_RETRY:
        log.debug(f"[{trace_id}] Non-retryable error, stopping")
        return False

    delay_ms = calculate_delay_ms(strategy, base_ms, max_ms, attempt)

    log.info(
        f"[{trace_id}] ⏱️ Retry with {strategy.value}: "
        f"attempt={attempt + 1}, delay={delay_ms}ms"
    )

    await asyncio.sleep(delay_ms / 1000.0)
    return True


def is_retryable_error(status_code: int) -> bool:
    """
    Check if an error status code is retryable.

    Args:
        status_code: HTTP status code

    Returns:
        True if the error should be retried
    """
    strategy, _, _ = determine_retry_strategy(status_code)
    return strategy != RetryStrategy.NO_RETRY
