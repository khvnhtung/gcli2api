import asyncio
import base64
import hashlib
import json
from collections import OrderedDict
from typing import Any, Optional

from config import (
    get_image_offload_cache_size,
    get_image_offload_timeout_seconds,
)
from log import log
from src.api.antigravity import non_stream_request as antigravity_non_stream
from src.api.geminicli import non_stream_request as geminicli_non_stream
from src.utils import DEFAULT_SAFETY_SETTINGS


_PROMPT = (
    "Describe this image in 1-2 short factual sentences for LLM context compression. "
    "Include only salient visual information. No markdown, no bullets, no speculation."
)

_MAX_CONTEXT_CHARS = 320

_cache_lock = asyncio.Lock()
_cache: "OrderedDict[str, str]" = OrderedDict()
_inflight: dict[str, asyncio.Task] = {}


def _normalize_space(text: str) -> str:
    return " ".join((text or "").split())


def _truncate(text: str, limit: int) -> str:
    compact = _normalize_space(text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)

    return "\n".join(parts)


def _build_context_hint(messages: list[Any], current_index: int) -> str:
    """
    Build compact context for image replacement text.

    Includes current message text and nearest previous user/assistant texts.
    """
    if current_index < 0 or current_index >= len(messages):
        return ""

    current = messages[current_index]
    current_text = ""
    if isinstance(current, dict):
        current_text = _extract_text_from_content(current.get("content"))

    prev_user = ""
    prev_assistant = ""

    for idx in range(current_index - 1, -1, -1):
        message = messages[idx]
        if not isinstance(message, dict):
            continue

        role = str(message.get("role") or "")
        text = _extract_text_from_content(message.get("content"))
        if not text.strip():
            continue

        if role == "user" and not prev_user:
            prev_user = text
        elif role in ("assistant", "model") and not prev_assistant:
            prev_assistant = text

        if prev_user and prev_assistant:
            break

    segments: list[str] = []
    if current_text.strip():
        segments.append(f"request={_truncate(current_text, _MAX_CONTEXT_CHARS)}")
    if prev_user.strip():
        segments.append(f"prev_user={_truncate(prev_user, _MAX_CONTEXT_CHARS)}")
    if prev_assistant.strip():
        segments.append(f"prev_assistant={_truncate(prev_assistant, _MAX_CONTEXT_CHARS)}")

    return " | ".join(segments)


def _extract_caption_from_response(resp_obj: dict[str, Any]) -> str:
    data = resp_obj.get("response", resp_obj)
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    parts = first.get("content", {}).get("parts", [])
    text_parts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            text_parts.append(str(part["text"]))
    text = " ".join(text_parts)
    text = " ".join(text.split())
    return text[:400]


def _hash_image(media_type: str, data_b64: str) -> str:
    hasher = hashlib.sha256()
    hasher.update(media_type.encode("utf-8", errors="ignore"))
    hasher.update(b":")
    try:
        raw = base64.b64decode(data_b64, validate=False)
        hasher.update(raw)
    except Exception:
        hasher.update(data_b64.encode("utf-8", errors="ignore"))
    return hasher.hexdigest()


async def _trim_cache_if_needed() -> None:
    max_size = max(1, await get_image_offload_cache_size())
    while len(_cache) > max_size:
        _cache.popitem(last=False)


async def _describe_image(media_type: str, data_b64: str) -> Optional[str]:
    timeout_s = await get_image_offload_timeout_seconds()

    def _build_body(model_name: str) -> dict[str, Any]:
        return {
            "model": model_name,
            "request": {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": _PROMPT},
                            {
                                "inlineData": {
                                    "mimeType": media_type,
                                    "data": data_b64,
                                }
                            },
                        ],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 160,
                },
                "safetySettings": DEFAULT_SAFETY_SETTINGS,
            },
        }

    # Fixed fallback chain requested by user:
    # 1) Antigravity gemini-3-flash
    # 2) GeminiCLI gemini-2.5-flash
    attempts = [
        ("antigravity", antigravity_non_stream, "gemini-3-flash"),
        ("geminicli", geminicli_non_stream, "gemini-2.5-flash"),
    ]

    for route_name, executor, model_name in attempts:
        try:
            resp = await asyncio.wait_for(executor(body=_build_body(model_name)), timeout=timeout_s)
            status_code = int(getattr(resp, "status_code", 0) or 0)

            body_text = ""
            if hasattr(resp, "body"):
                raw = resp.body
                if isinstance(raw, (bytes, bytearray)):
                    body_text = raw.decode("utf-8", errors="ignore")
                else:
                    body_text = str(raw)
            else:
                body_text = str(resp)

            if status_code and status_code >= 400:
                log.warning(
                    f"[IMAGE_OFFLOAD] {route_name} caption request failed "
                    f"(model={model_name}, status={status_code})"
                )
                continue

            resp_obj = json.loads(body_text)
            caption = _extract_caption_from_response(resp_obj)
            if caption:
                return caption

            log.warning(f"[IMAGE_OFFLOAD] {route_name} returned empty caption (model={model_name})")
        except Exception as e:
            log.warning(f"[IMAGE_OFFLOAD] {route_name} caption error (model={model_name}): {e}")

    return None


async def _get_caption_cached(media_type: str, data_b64: str) -> Optional[str]:
    image_hash = _hash_image(media_type, data_b64)

    async with _cache_lock:
        cached = _cache.get(image_hash)
        if cached:
            _cache.move_to_end(image_hash)
            return cached

        inflight_task = _inflight.get(image_hash)
        if inflight_task is None:
            inflight_task = asyncio.create_task(_describe_image(media_type, data_b64))
            _inflight[image_hash] = inflight_task

    caption: Optional[str]
    try:
        caption = await inflight_task
    except Exception:
        caption = None

    async with _cache_lock:
        current = _inflight.get(image_hash)
        if current is inflight_task:
            _inflight.pop(image_hash, None)

        if caption:
            _cache[image_hash] = caption
            _cache.move_to_end(image_hash)
            await _trim_cache_if_needed()

    return caption


async def process_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Replace Anthropic image blocks with text descriptions.

    If description generation fails, keep original image blocks unchanged.
    """
    if not isinstance(payload, dict):
        return payload

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload

    new_payload = dict(payload)
    new_messages: list[dict[str, Any]] = []
    image_count = 0
    replaced_count = 0

    for msg_idx, message in enumerate(messages):
        if not isinstance(message, dict):
            new_messages.append(message)
            continue

        content = message.get("content")
        if not isinstance(content, list):
            new_messages.append(message)
            continue

        context_hint = _build_context_hint(messages, msg_idx)

        new_content: list[Any] = []
        changed = False

        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image":
                new_content.append(block)
                continue

            source = block.get("source") or {}
            if source.get("type") != "base64":
                new_content.append(block)
                continue

            media_type = str(source.get("media_type") or "image/png")
            data_b64 = source.get("data")
            if not isinstance(data_b64, str) or not data_b64:
                new_content.append(block)
                continue

            image_count += 1
            caption = await _get_caption_cached(media_type, data_b64)
            if caption:
                replacement = f"[Image description: {caption}]"
                if context_hint:
                    replacement += f" [Context: {context_hint}]"
                new_content.append({"type": "text", "text": replacement})
                replaced_count += 1
                changed = True
            else:
                new_content.append(block)

        if changed:
            new_msg = dict(message)
            new_msg["content"] = new_content
            new_messages.append(new_msg)
        else:
            new_messages.append(message)

    new_payload["messages"] = new_messages

    if image_count > 0:
        log.info(
            f"[IMAGE_OFFLOAD] images={image_count}, replaced={replaced_count}, "
            f"kept={image_count - replaced_count}"
        )

    return new_payload
