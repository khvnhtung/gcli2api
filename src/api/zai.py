"""
Z.AI API client (OpenAI-compatible chat completions).
"""

from typing import Any, Dict, List, Optional

from config import (
    get_zai_api_key,
    get_zai_base_url,
    get_zai_enabled,
    get_zai_model,
    get_zai_timeout_seconds,
)
from log import log
from src.httpx_client import post_async


class ZAIRequestError(Exception):
    def __init__(self, status_code: int, message: str, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


def _auth_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _extract_error_message(response_text: str) -> str:
    if not response_text:
        return "empty error response"

    try:
        import json

        payload = json.loads(response_text)
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or response_text)
        if isinstance(err, str):
            return err
        if payload.get("message"):
            return str(payload.get("message"))
        return response_text
    except Exception:
        return response_text


async def is_zai_ready() -> bool:
    if not await get_zai_enabled():
        return False
    api_key = (await get_zai_api_key()).strip()
    return bool(api_key)


async def chat_completions(
    *,
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    thinking_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Call Z.AI OpenAI-compatible /chat/completions endpoint."""
    if not await get_zai_enabled():
        raise ZAIRequestError(503, "Z.AI provider is disabled")

    api_key = (await get_zai_api_key()).strip()
    if not api_key:
        raise ZAIRequestError(503, "Z.AI API key is not configured")

    base_url = await get_zai_base_url()
    target_model = (model or await get_zai_model()).strip()
    timeout = await get_zai_timeout_seconds()

    payload: Dict[str, Any] = {
        "model": target_model,
        "messages": messages,
        "stream": False,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if thinking_enabled is not None:
        payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}

    url = f"{base_url}/chat/completions"
    resp = await post_async(
        url,
        json=payload,
        headers=_auth_headers(api_key),
        timeout=timeout,
    )

    if resp.status_code != 200:
        body_text = resp.text
        msg = _extract_error_message(body_text)
        log.warning(f"[ZAI] chat completion failed status={resp.status_code}, msg={msg[:240]}")
        raise ZAIRequestError(resp.status_code, msg, body_text)

    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except Exception as e:
        raise ZAIRequestError(502, f"Failed to parse Z.AI response JSON: {e}", resp.text) from e
