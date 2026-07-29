"""
Optional request offload to Z.AI for low-risk utility prompts.
"""

import json
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from fastapi.responses import JSONResponse, StreamingResponse

from config import (
    get_zai_base_url,
    get_zai_model,
    get_zai_model_a,
    get_zai_offload_enabled,
    get_zai_offload_target_model,
)
from log import log
from src.api.zai import ZAIRequestError, chat_completions, is_zai_ready
from src.converter.fake_stream import (
    build_anthropic_fake_stream_chunks,
    build_openai_fake_stream_chunks,
    format_sse,
)


UTILITY_PATTERNS = (
    "analyze if this message indicates a new conversation topic",
    "extract any file paths that this command reads or modifies",
    "you are a title generator. you output only a thread title",
    "summarize this coding conversation in under 50 characters",
)

def _is_search_request(payload: Dict[str, Any], real_model: str) -> bool:
    model_lower = (real_model or "").lower()
    if "-search" in model_lower:
        return True

    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False

    for tool in tools:
        if not isinstance(tool, dict):
            continue

        # Anthropic web_search tool format
        tool_type = str(tool.get("type") or "").lower()
        tool_name = str(tool.get("name") or "").lower()
        if "web_search" in tool_type or "web_search" in tool_name:
            return True

        # Gemini native tool format
        if "googleSearch" in tool:
            return True

    return False


def _to_text_content(content: Any) -> Optional[str]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    text_parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            return None

        item_type = item.get("type")
        if item_type in ("text", "input_text"):
            text_val = item.get("text")
            if not isinstance(text_val, str):
                return None
            text_parts.append(text_val)
            continue

        # Anthropic text blocks may omit type in some client payloads.
        if item_type is None and isinstance(item.get("text"), str):
            text_parts.append(item["text"])
            continue

        return None

    return "\n".join(text_parts)


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _looks_utility_prompt(full_text: str) -> bool:
    lower = full_text.lower()
    if _first_non_empty_line(full_text).lower() == "count":
        return True
    return any(p in lower for p in UTILITY_PATTERNS)


def _extract_openai_messages_for_zai(payload: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None

    converted: List[Dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            return None
        role = msg.get("role")
        if role not in ("system", "user", "assistant"):
            return None
        if msg.get("tool_calls") or msg.get("tool_call_id"):
            return None

        content = _to_text_content(msg.get("content"))
        if content is None:
            return None

        converted.append({"role": role, "content": content})

    return converted


def _extract_anthropic_messages_for_zai(payload: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None

    converted: List[Dict[str, str]] = []

    system = payload.get("system")
    if system is not None:
        system_text = _to_text_content(system)
        if system_text is None:
            return None
        converted.append({"role": "system", "content": system_text})

    for msg in messages:
        if not isinstance(msg, dict):
            return None
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return None

        content = _to_text_content(msg.get("content"))
        if content is None:
            return None

        converted.append({"role": role, "content": content})

    return converted


def _extract_zai_text_and_meta(zai_response: Dict[str, Any]) -> Tuple[str, str, str, Dict[str, int]]:
    choices = zai_response.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", "", "stop", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    first_raw = choices[0]
    first: Dict[str, Any] = first_raw if isinstance(first_raw, dict) else {}

    message_raw = first.get("message")
    message: Dict[str, Any] = message_raw if isinstance(message_raw, dict) else {}

    content_raw = message.get("content")
    content = content_raw if isinstance(content_raw, str) else ""

    reasoning_raw = message.get("reasoning_content")
    reasoning = reasoning_raw if isinstance(reasoning_raw, str) else ""

    finish_reason_raw = first.get("finish_reason")
    finish_reason = finish_reason_raw if isinstance(finish_reason_raw, str) else "stop"

    usage = zai_response.get("usage") if isinstance(zai_response.get("usage"), dict) else {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))

    return content, reasoning, finish_reason, {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _finish_reason_openai_to_gemini(finish_reason: str) -> str:
    if finish_reason == "length":
        return "MAX_TOKENS"
    return "STOP"


def _finish_reason_openai_to_anthropic(finish_reason: str) -> str:
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"


async def _log_zai_audit(
    *,
    mode: str,
    is_streaming: bool,
    request_payload: Dict[str, Any],
    real_model: str,
    zai_model: str,
    http_status: int,
    outcome: str,
    error_text: str = "",
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    fallback_used: bool = False,
    fallback_detail: Optional[str] = None,
    response_payload: Optional[Any] = None,
) -> None:
    try:
        from src.audit_log import generate_request_id, get_audit_context, log_attempt

        ctx = get_audit_context()
        request_id = (ctx.get("request_id") if ctx else None) or generate_request_id()
        start_time = ctx.get("start_time") if ctx else None

        await log_attempt(
            request_id=request_id,
            mode=(ctx.get("mode") if ctx else None) or mode,
            model_requested=(ctx.get("model_requested") if ctx else None) or real_model,
            model_effective=zai_model,
            endpoint_base=await get_zai_base_url(),
            route_provider="zai",
            route_policy="utility_offload_allowlist",
            route_reason="utility_prompt",
            fallback_used=fallback_used,
            fallback_detail=fallback_detail,
            streaming=(ctx.get("streaming") if ctx else None) or is_streaming,
            attempt_no=(ctx.get("attempt_no") if ctx else None) or 0,
            max_retries=(ctx.get("max_retries") if ctx else None) or 0,
            http_status=http_status,
            latency_ms=(time.time() - start_time) * 1000 if start_time else None,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            error_text=error_text,
            outcome=outcome,
            request_payload=request_payload,
            response_payload=response_payload,
        )
    except Exception:
        # Keep offload path best-effort.
        return


async def _offload_enabled_for_model(real_model: str) -> bool:
    if not await get_zai_offload_enabled():
        return False
    if not await is_zai_ready():
        return False
    target = (await get_zai_offload_target_model()).strip()
    if target and real_model != target:
        return False
    return True


async def _select_model_for_utility() -> str:
    model_a = (await get_zai_model_a()).strip()
    if model_a:
        return model_a
    return await get_zai_model()


async def maybe_offload_openai_to_zai(
    *,
    payload: Dict[str, Any],
    real_model: str,
    is_streaming: bool,
    route_label: str,
) -> Optional[Any]:
    mode = "antigravity" if route_label.startswith("ANTIGRAVITY") else "geminicli"

    if not await _offload_enabled_for_model(real_model):
        return None
    if _is_search_request(payload, real_model):
        return None
    if payload.get("tools"):
        return None

    zai_messages = _extract_openai_messages_for_zai(payload)
    if not zai_messages:
        return None

    full_text = "\n\n".join(m.get("content", "") for m in zai_messages)
    if not _looks_utility_prompt(full_text):
        return None

    zai_model = await _select_model_for_utility()

    try:
        zai_response = await chat_completions(
            messages=zai_messages,
            model=zai_model,
            temperature=payload.get("temperature"),
            max_tokens=payload.get("max_tokens"),
            thinking_enabled=False,
        )
    except ZAIRequestError as e:
        log.warning(
            f"[{route_label}] Z.AI offload failed status={e.status_code}: {str(e)[:240]}"
        )
        await _log_zai_audit(
            mode=mode,
            is_streaming=is_streaming,
            request_payload=payload,
            real_model=real_model,
            zai_model=zai_model,
            http_status=e.status_code,
            outcome="failed",
            error_text=str(e),
            fallback_used=True,
            fallback_detail="zai->google",
            response_payload=e.response_text,
        )
        return None
    except Exception as e:
        log.warning(f"[{route_label}] Z.AI offload failed: {e}")
        await _log_zai_audit(
            mode=mode,
            is_streaming=is_streaming,
            request_payload=payload,
            real_model=real_model,
            zai_model=zai_model,
            http_status=502,
            outcome="failed",
            error_text=str(e),
            fallback_used=True,
            fallback_detail="zai->google",
        )
        return None

    content, reasoning, finish_reason, usage = _extract_zai_text_and_meta(zai_response)
    if not content and not reasoning:
        return None

    log.info(
        f"[{route_label}] Offloaded utility request to Z.AI model={zai_model}, "
        f"prompt_tokens={usage.get('prompt_tokens', 0)}"
    )
    await _log_zai_audit(
        mode=mode,
        is_streaming=is_streaming,
        request_payload=payload,
        real_model=real_model,
        zai_model=zai_model,
        http_status=200,
        outcome="success",
        tokens_in=usage.get("prompt_tokens", 0),
        tokens_out=usage.get("completion_tokens", 0),
        response_payload=zai_response,
    )

    if not is_streaming:
        response = {
            "id": zai_response.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(zai_response.get("created") or time.time()),
            "model": real_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        **({"reasoning_content": reasoning} if reasoning else {}),
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }
        return JSONResponse(content=response)

    gemini_finish = _finish_reason_openai_to_gemini(finish_reason)
    chunks = build_openai_fake_stream_chunks(content, reasoning, gemini_finish, real_model)

    async def _stream() -> AsyncGenerator[bytes, None]:
        for chunk in chunks:
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


async def maybe_offload_anthropic_to_zai(
    *,
    payload: Dict[str, Any],
    real_model: str,
    is_streaming: bool,
    route_label: str,
) -> Optional[Any]:
    mode = "antigravity" if route_label.startswith("ANTIGRAVITY") else "geminicli"

    if not await _offload_enabled_for_model(real_model):
        return None
    if _is_search_request(payload, real_model):
        return None
    if payload.get("tools"):
        return None

    zai_messages = _extract_anthropic_messages_for_zai(payload)
    if not zai_messages:
        return None

    full_text = "\n\n".join(m.get("content", "") for m in zai_messages)
    if not _looks_utility_prompt(full_text):
        return None

    zai_model = await _select_model_for_utility()

    try:
        zai_response = await chat_completions(
            messages=zai_messages,
            model=zai_model,
            temperature=payload.get("temperature"),
            max_tokens=payload.get("max_tokens"),
            thinking_enabled=False,
        )
    except ZAIRequestError as e:
        log.warning(
            f"[{route_label}] Z.AI offload failed status={e.status_code}: {str(e)[:240]}"
        )
        await _log_zai_audit(
            mode=mode,
            is_streaming=is_streaming,
            request_payload=payload,
            real_model=real_model,
            zai_model=zai_model,
            http_status=e.status_code,
            outcome="failed",
            error_text=str(e),
            fallback_used=True,
            fallback_detail="zai->google",
            response_payload=e.response_text,
        )
        return None
    except Exception as e:
        log.warning(f"[{route_label}] Z.AI offload failed: {e}")
        await _log_zai_audit(
            mode=mode,
            is_streaming=is_streaming,
            request_payload=payload,
            real_model=real_model,
            zai_model=zai_model,
            http_status=502,
            outcome="failed",
            error_text=str(e),
            fallback_used=True,
            fallback_detail="zai->google",
        )
        return None

    content, reasoning, finish_reason, usage = _extract_zai_text_and_meta(zai_response)
    if not content and not reasoning:
        return None

    log.info(
        f"[{route_label}] Offloaded utility request to Z.AI model={zai_model}, "
        f"prompt_tokens={usage.get('prompt_tokens', 0)}"
    )
    await _log_zai_audit(
        mode=mode,
        is_streaming=is_streaming,
        request_payload=payload,
        real_model=real_model,
        zai_model=zai_model,
        http_status=200,
        outcome="success",
        tokens_in=usage.get("prompt_tokens", 0),
        tokens_out=usage.get("completion_tokens", 0),
        response_payload=zai_response,
    )

    if not is_streaming:
        response_content: List[Dict[str, Any]] = []
        if reasoning:
            response_content.append({"type": "thinking", "thinking": reasoning})
        if content:
            response_content.append({"type": "text", "text": content})

        response = {
            "id": zai_response.get("id") or f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": real_model,
            "content": response_content,
            "stop_reason": _finish_reason_openai_to_anthropic(finish_reason),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }
        return JSONResponse(content=response)

    gemini_finish = _finish_reason_openai_to_gemini(finish_reason)
    chunks = build_anthropic_fake_stream_chunks(content, reasoning, gemini_finish, real_model)

    async def _stream() -> AsyncGenerator[bytes, None]:
        for chunk in chunks:
            yield format_sse(chunk)

    return StreamingResponse(_stream(), media_type="text/event-stream")
