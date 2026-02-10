"""Token estimation using Gemini's native countTokens API with local fallback."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from log import log


# ==================== Usage scaling for Claude Code compatibility ====================

# Real context windows per model family.
# Claude models: 200K (enforced by Vertex AI upstream) — no scaling needed.
# Gemini models: 1M-2M — must scale down to 200K for Claude Code.
_GEMINI_CONTEXT_LIMITS: Dict[str, int] = {
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-3-flash": 1_048_576,
    "gemini-3-pro": 2_097_152,
}

CLAUDE_CODE_CONTEXT_WINDOW = 200_000


def _get_gemini_context_limit(model: str) -> Optional[int]:
    """Return the real context limit for a Gemini model, or None if not Gemini."""
    model_lower = model.lower()
    if "claude" in model_lower:
        return None
    for prefix, limit in _GEMINI_CONTEXT_LIMITS.items():
        if prefix in model_lower:
            return limit
    if "gemini" in model_lower:
        return 1_048_576
    return None


def scale_usage_tokens(
    input_tokens: int, output_tokens: int, model: str
) -> Tuple[int, int]:
    """Scale token counts so Gemini's 1M+ context maps to Claude Code's 200K window.

    Claude models (200K real limit) pass through unscaled.
    Gemini models (1M+) get linearly scaled: raw * 200K / real_limit.
    """
    context_limit = _get_gemini_context_limit(model)
    if context_limit is None or context_limit <= CLAUDE_CODE_CONTEXT_WINDOW:
        return input_tokens, output_tokens

    ratio = CLAUDE_CODE_CONTEXT_WINDOW / context_limit
    scaled_input = int(input_tokens * ratio)
    # Output tokens stay unscaled — they're small and don't drive compaction
    return scaled_input, output_tokens


# ==================== Native countTokens via Gemini API ====================


def _anthropic_messages_to_gemini_contents(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Lightweight conversion of Anthropic messages to Gemini contents for counting only."""
    contents: List[Dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        gemini_role = "model" if role == "assistant" else "user"
        parts: List[Dict[str, Any]] = []

        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                parts.append({"text": content})
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")
                if item_type == "text":
                    text = item.get("text", "")
                    if text.strip():
                        parts.append({"text": text})
                elif item_type == "image":
                    source = item.get("source") or {}
                    if source.get("type") == "base64":
                        parts.append({
                            "inlineData": {
                                "mimeType": source.get("media_type", "image/png"),
                                "data": source.get("data", ""),
                            }
                        })
                elif item_type == "thinking":
                    text = item.get("thinking", "")
                    if text.strip():
                        parts.append({"text": text, "thought": True})
                elif item_type == "tool_use":
                    parts.append({
                        "functionCall": {
                            "name": item.get("name", ""),
                            "args": item.get("input", {}),
                        }
                    })
                elif item_type == "tool_result":
                    result_content = item.get("content", "")
                    if isinstance(result_content, str):
                        result_text = result_content
                    elif isinstance(result_content, list):
                        result_text = " ".join(
                            b.get("text", "") for b in result_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        result_text = str(result_content)
                    parts.append({
                        "functionResponse": {
                            "name": item.get("tool_use_id", ""),
                            "response": {"result": result_text},
                        }
                    })

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    return contents


def _payload_has_images(payload: Dict[str, Any]) -> bool:
    """Check if the payload contains any image content blocks."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image":
                return True
    return False


async def count_tokens_native(
    payload: Dict[str, Any],
) -> Optional[int]:
    """Count tokens via Gemini's native countTokens API.

    Returns totalTokens on success, None on failure (caller should fall back).
    Skips native counting for payloads with images since the API counts base64
    bytes as tokens, inflating the count massively.
    """
    from config import get_code_assist_endpoint
    from src.credential_manager import credential_manager
    from src.httpx_client import post_async

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None

    # Native countTokens counts base64 image data as raw tokens — skip to local fallback
    if _payload_has_images(payload):
        log.debug("[TOKEN_COUNT] Payload has images, skipping native (would inflate count)")
        return None

    # Convert Anthropic messages → Gemini contents
    try:
        contents = _anthropic_messages_to_gemini_contents(messages)
    except Exception as e:
        log.debug(f"[TOKEN_COUNT] Failed to convert messages: {e}")
        return None

    if not contents:
        return None

    # Get a credential (prefer geminicli, fall back to antigravity)
    try:
        cred_result = await credential_manager.get_valid_credential(mode="geminicli")
        if not cred_result:
            cred_result = await credential_manager.get_valid_credential(mode="antigravity")
        if not cred_result:
            log.debug("[TOKEN_COUNT] No credentials available for native countTokens")
            return None

        _, cred_data = cred_result
        token = cred_data.get("token") or cred_data.get("access_token", "")
        if not token:
            return None
    except Exception as e:
        log.debug(f"[TOKEN_COUNT] Failed to get credential: {e}")
        return None

    # Call native countTokens
    try:
        endpoint = await get_code_assist_endpoint()
        url = f"{endpoint}/v1internal:countTokens"

        resp = await post_async(
            url,
            json={"request": {"contents": contents}},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

        if resp.status_code == 200:
            data = resp.json()
            content_tokens = data.get("totalTokens", 0)

            # Native endpoint only counts contents, not system/tools.
            # Add a rough estimate for those (text-only, no images → chars/4 is safe).
            extra = _estimate_system_and_tools(payload)
            total = content_tokens + extra

            log.debug(
                f"[TOKEN_COUNT] Native: contents={content_tokens}, "
                f"system+tools={extra}, total={total}"
            )
            return total
        else:
            log.debug(
                f"[TOKEN_COUNT] Native countTokens failed: "
                f"status={resp.status_code}, body={resp.text[:200]}"
            )
            return None

    except Exception as e:
        log.debug(f"[TOKEN_COUNT] Native countTokens error: {e}")
        return None


def _estimate_system_and_tools(payload: Dict[str, Any]) -> int:
    """Estimate tokens for system prompt and tools (text-only, no images)."""
    chars = 0

    system = payload.get("system")
    if isinstance(system, str):
        chars += len(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                chars += len(block.get("text", ""))

    tools = payload.get("tools")
    if isinstance(tools, list):
        import json as _json
        chars += len(_json.dumps(tools, ensure_ascii=False))

    return chars // 4


# ==================== Local fallback estimator ====================


def estimate_input_tokens(payload: Dict[str, Any]) -> int:
    """Local fallback: chars/4 for text, fixed cost per image.

    Skips base64 data fields to avoid inflating counts for images.
    """
    total_chars = 0
    image_count = 0

    def _walk(obj: Any, inside_image: bool = False) -> None:
        nonlocal total_chars, image_count
        if isinstance(obj, str):
            if not inside_image:
                total_chars += len(obj)
        elif isinstance(obj, dict):
            is_image = obj.get("type") == "image" or "inlineData" in obj
            if is_image:
                image_count += 1
            for k, v in obj.items():
                # Skip base64 data fields inside image blocks
                if is_image and k in ("data", "source"):
                    if k == "source" and isinstance(v, dict):
                        # Count non-data fields in source (media_type etc)
                        for sk, sv in v.items():
                            if sk != "data":
                                _walk(sv, inside_image=True)
                    continue
                _walk(v, inside_image=is_image or inside_image)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, inside_image)

    _walk(payload)
    return max(1, total_chars // 4 + image_count * 300)
