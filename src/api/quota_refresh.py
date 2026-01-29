"""Realtime quota refresh helpers.

This module implements a lightweight subset of Antigravity-Manager's approach:

- When a 429 happens but the error body does not include explicit cooldown timing
  (Retry-After / quotaResetTimeStamp / quotaResetDelay), we can call
  `v1internal:fetchAvailableModels` to retrieve `quotaInfo.resetTime`.

We keep this logic isolated to avoid cyclic imports with src/api/antigravity.py.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from log import log

from src.httpx_client import post_async


# In-memory cache: (cache_key, model_name) -> (expires_at, reset_ts)
_CACHE: dict[Tuple[str, str], Tuple[float, float]] = {}


def _parse_iso_to_timestamp(reset_time: str) -> Optional[float]:
    if not reset_time or not isinstance(reset_time, str):
        return None
    s = reset_time.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def extract_quota_reset_timestamp_from_fetch_models(
    data: Dict[str, Any],
    model_name: str,
    *,
    fallback_to_earliest: bool,
) -> Optional[float]:
    """Extract reset timestamp from fetchAvailableModels response.

    Expected shape (partial):
      {
        "models": {
          "gemini-3-flash": {"quotaInfo": {"resetTime": "2026-...Z"}},
          ...
        }
      }
    """
    if not isinstance(data, dict):
        return None
    models = data.get("models")
    if not isinstance(models, dict) or not models:
        return None

    def _extract_model_reset_ts(model_info: Any) -> Optional[float]:
        if not isinstance(model_info, dict):
            return None
        quota = model_info.get("quotaInfo")
        if not isinstance(quota, dict):
            return None
        return _parse_iso_to_timestamp(quota.get("resetTime") or "")

    # Prefer target model's resetTime.
    target_info = models.get(model_name)
    ts = _extract_model_reset_ts(target_info)
    if ts is not None:
        return ts

    if not fallback_to_earliest:
        return None

    # Conservative fallback: earliest reset among all models.
    earliest: Optional[float] = None
    for _name, info in models.items():
        t = _extract_model_reset_ts(info)
        if t is None:
            continue
        if earliest is None or t < earliest:
            earliest = t
    return earliest


async def fetch_realtime_quota_reset_timestamp(
    *,
    api_base_url: str,
    headers: Dict[str, str],
    model_name: str,
    cache_key: str,
    cache_ttl_seconds: int,
    timeout_seconds: float,
    fallback_to_earliest: bool,
) -> Optional[float]:
    now = time.time()

    if cache_ttl_seconds > 0:
        cache_key_tuple = (str(cache_key), str(model_name))
        cached = _CACHE.get(cache_key_tuple)
        if cached:
            expires_at, reset_ts = cached
            if now < float(expires_at):
                return float(reset_ts)
            _CACHE.pop(cache_key_tuple, None)

    url = f"{api_base_url}/v1internal:fetchAvailableModels"
    try:
        resp = await post_async(url=url, json={}, headers=headers, timeout=float(timeout_seconds))
    except Exception as e:
        log.warning(f"[QUOTA] fetchAvailableModels request failed: {e}")
        return None

    if getattr(resp, "status_code", 0) != 200:
        try:
            txt = resp.text
        except Exception:
            txt = ""
        log.warning(f"[QUOTA] fetchAvailableModels non-200: {resp.status_code} {txt[:200]}")
        return None

    try:
        data = resp.json()
    except Exception as e:
        log.warning(f"[QUOTA] fetchAvailableModels JSON parse failed: {e}")
        return None

    reset_ts = extract_quota_reset_timestamp_from_fetch_models(
        data,
        model_name,
        fallback_to_earliest=fallback_to_earliest,
    )
    if reset_ts is None:
        return None

    if cache_ttl_seconds > 0:
        _CACHE[(str(cache_key), str(model_name))] = (now + float(cache_ttl_seconds), float(reset_ts))

    return float(reset_ts)
