"""
Structured Error Classes for gcli2api

Provides typed error classes for better error handling and classification.
Ported from antigravity-claude-proxy for improved error management.
"""

from typing import Any, Dict, Optional


class GcliApiError(Exception):
    """
    Base error class for gcli2api errors.

    Attributes:
        message: Human-readable error message
        code: Machine-readable error code for programmatic handling
        retryable: Whether this error can be retried
        metadata: Additional context about the error
    """

    def __init__(
        self,
        message: str,
        code: str,
        retryable: bool = False,
        metadata: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "name": self.__class__.__name__,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            **self.metadata
        }


class RateLimitError(GcliApiError):
    """
    Rate limit error (429 / RESOURCE_EXHAUSTED).

    Indicates the user or account has exceeded their quota.
    """

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        reset_ms: Optional[int] = None,
        credential_file: Optional[str] = None
    ):
        super().__init__(
            message=message,
            code="RATE_LIMITED",
            retryable=True,
            metadata={"reset_ms": reset_ms, "credential_file": credential_file}
        )
        self.reset_ms = reset_ms
        self.credential_file = credential_file


class CapacityExhaustedError(GcliApiError):
    """
    Capacity exhausted error - model is at capacity (not user quota).

    Different from RateLimitError: this is a temporary infrastructure limit
    that should be retried on the SAME account with shorter delays.
    """

    def __init__(
        self,
        message: str = "Model capacity exhausted",
        retry_after_ms: Optional[int] = None
    ):
        super().__init__(
            message=message,
            code="CAPACITY_EXHAUSTED",
            retryable=True,
            metadata={"retry_after_ms": retry_after_ms}
        )
        self.retry_after_ms = retry_after_ms


class AuthError(GcliApiError):
    """
    Authentication error (invalid credentials, token expired, etc.).

    Non-retryable - requires credential refresh or replacement.
    """

    def __init__(
        self,
        message: str = "Authentication failed",
        credential_file: Optional[str] = None,
        reason: Optional[str] = None
    ):
        super().__init__(
            message=message,
            code="AUTH_INVALID",
            retryable=False,
            metadata={"credential_file": credential_file, "reason": reason}
        )
        self.credential_file = credential_file
        self.reason = reason


class NoCredentialsError(GcliApiError):
    """
    No credentials available error.

    Retryable only if all credentials are rate-limited (will recover).
    """

    def __init__(
        self,
        message: str = "No credentials available",
        all_rate_limited: bool = False
    ):
        super().__init__(
            message=message,
            code="NO_CREDENTIALS",
            retryable=all_rate_limited,
            metadata={"all_rate_limited": all_rate_limited}
        )
        self.all_rate_limited = all_rate_limited


class MaxRetriesError(GcliApiError):
    """
    Max retries exceeded error.

    All retry attempts have been exhausted.
    """

    def __init__(
        self,
        message: str = "Max retries exceeded",
        attempts: int = 0,
        last_error: Optional[str] = None
    ):
        super().__init__(
            message=message,
            code="MAX_RETRIES",
            retryable=False,
            metadata={"attempts": attempts, "last_error": last_error}
        )
        self.attempts = attempts
        self.last_error = last_error


class ApiError(GcliApiError):
    """
    API error from upstream service.

    Retryable for 5xx errors, non-retryable for 4xx.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        error_type: str = "api_error"
    ):
        super().__init__(
            message=message,
            code=error_type.upper(),
            retryable=status_code >= 500,
            metadata={"status_code": status_code, "error_type": error_type}
        )
        self.status_code = status_code
        self.error_type = error_type


class EmptyResponseError(GcliApiError):
    """
    Empty response error - API returned no content.

    Retryable - often a transient issue.
    """

    def __init__(self, message: str = "No content received from API"):
        super().__init__(
            message=message,
            code="EMPTY_RESPONSE",
            retryable=True
        )


class ConversionError(GcliApiError):
    """
    Format conversion error.

    Raised when converting between API formats fails.
    """

    def __init__(
        self,
        message: str,
        source_format: Optional[str] = None,
        target_format: Optional[str] = None
    ):
        super().__init__(
            message=message,
            code="CONVERSION_ERROR",
            retryable=False,
            metadata={"source_format": source_format, "target_format": target_format}
        )
        self.source_format = source_format
        self.target_format = target_format


# ============================================================================
# Error Detection Helper Functions
# ============================================================================

def is_rate_limit_error(error: Exception) -> bool:
    """
    Check if an error is a rate limit error.
    Works with both custom error classes and legacy string-based errors.
    """
    if isinstance(error, RateLimitError):
        return True
    msg = (getattr(error, "message", "") or str(error)).lower()
    return (
        "429" in msg or
        "resource_exhausted" in msg or
        "quota_exhausted" in msg or
        "rate limit" in msg
    )


def is_capacity_exhausted_error(error: Exception) -> bool:
    """
    Check if an error is a capacity exhausted error (model overload, not user quota).
    """
    if isinstance(error, CapacityExhaustedError):
        return True
    msg = (getattr(error, "message", "") or str(error)).lower()
    return (
        "model_capacity_exhausted" in msg or
        "capacity_exhausted" in msg or
        "model is currently overloaded" in msg or
        "service temporarily unavailable" in msg
    )


def is_auth_error(error: Exception) -> bool:
    """
    Check if an error is an authentication error.
    """
    if isinstance(error, AuthError):
        return True
    msg = (getattr(error, "message", "") or str(error)).upper()
    return (
        "AUTH_INVALID" in msg or
        "INVALID_GRANT" in msg or
        "TOKEN REFRESH FAILED" in msg or
        "TOKEN_REVOKED" in msg
    )


def is_empty_response_error(error: Exception) -> bool:
    """
    Check if an error is an empty response error.
    """
    return isinstance(error, EmptyResponseError)


def is_retryable_error(error: Exception) -> bool:
    """
    Check if an error is retryable.
    """
    if isinstance(error, GcliApiError):
        return error.retryable
    # For non-GcliApiError exceptions, check common patterns
    return is_rate_limit_error(error) or is_capacity_exhausted_error(error)


def classify_http_error(status_code: int, error_text: str = "") -> GcliApiError:
    """
    Classify an HTTP error into the appropriate error class.

    Args:
        status_code: HTTP status code
        error_text: Error response body text

    Returns:
        Appropriate GcliApiError subclass instance
    """
    error_lower = error_text.lower()

    if status_code == 429:
        # Check if it's capacity vs quota
        if is_capacity_exhausted_error(Exception(error_text)):
            return CapacityExhaustedError(message=error_text or "Model capacity exhausted")
        return RateLimitError(message=error_text or "Rate limit exceeded")

    elif status_code in (401, 403):
        return AuthError(message=error_text or "Authentication failed")

    elif status_code in (503, 529):
        return CapacityExhaustedError(message=error_text or "Service temporarily unavailable")

    elif status_code >= 500:
        return ApiError(
            message=error_text or f"Server error ({status_code})",
            status_code=status_code,
            error_type="server_error"
        )

    else:
        return ApiError(
            message=error_text or f"API error ({status_code})",
            status_code=status_code,
            error_type="client_error"
        )
