from __future__ import annotations

import time

import pytest

from src.api.quota_refresh import extract_quota_reset_timestamp_from_fetch_models
from src.api.utils import parse_and_log_cooldown, parse_quota_reset_time


def test_parse_quota_reset_time_uses_delay_when_timestamp_missing():
    now = time.time()
    data = {
        "error": {
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "metadata": {"quotaResetDelay": "2m30.1s"},
                }
            ]
        }
    }

    ts = parse_quota_reset_time(data)
    assert ts is not None
    # 2m30.1s => ceil => ~151s, allow small scheduling jitter.
    assert now + 149 <= ts <= now + 155


@pytest.mark.asyncio
async def test_parse_and_log_cooldown_prefers_retry_after_header():
    now = time.time()
    ts = await parse_and_log_cooldown("{}", mode="antigravity", retry_after_header="10")
    assert ts is not None
    assert now + 9 <= ts <= now + 12


def test_extract_quota_reset_timestamp_from_fetch_models_prefers_target_model():
    data = {
        "models": {
            "gemini-3-flash": {"quotaInfo": {"resetTime": "2026-01-29T00:00:00Z"}},
            "gemini-3-pro-high": {"quotaInfo": {"resetTime": "2026-01-30T00:00:00Z"}},
        }
    }
    ts = extract_quota_reset_timestamp_from_fetch_models(
        data, "gemini-3-flash", fallback_to_earliest=True
    )
    assert ts is not None


def test_extract_quota_reset_timestamp_from_fetch_models_can_fallback_to_earliest():
    data = {
        "models": {
            "a": {"quotaInfo": {"resetTime": "2026-01-30T00:00:00Z"}},
            "b": {"quotaInfo": {"resetTime": "2026-01-29T00:00:00Z"}},
        }
    }
    ts = extract_quota_reset_timestamp_from_fetch_models(data, "missing", fallback_to_earliest=True)
    assert ts is not None
