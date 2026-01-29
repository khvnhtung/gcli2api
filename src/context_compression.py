"""Context checkpoint compression for Anthropic->Antigravity.

Goal: prevent agentic clients (Claude Code/OpenCode) that resend full history + MCP
tool outputs from blowing up the upstream context window.

This module implements a minimal, high-impact "checkpoint + fork" approach:
- When estimated input tokens exceed a threshold, generate a dense state snapshot
  using a cheap model (default: gemini-3-flash).
- Replace most of the conversation history with the snapshot + a small tail.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from log import log

from src.converter.tool_result_compressor import compact_tool_result


# Hard cap for summary input to avoid summary-call prompt overflow.
MAX_TRANSCRIPT_CHARS = 1_200_000


STATE_SNAPSHOT_SYSTEM_PROMPT = """You are a context compression specialist.

You will be given a conversation transcript between a user and an AI coding assistant,
including tool calls and tool outputs.

Task: produce a single <state_snapshot> XML object that preserves all information
needed to continue the task correctly, while being extremely compact.

Rules:
- Output ONLY the <state_snapshot> XML. No extra commentary.
- Prefer stable references: file paths, identifiers, commands, URLs.
- Keep decisions, constraints, current plan, and any errors/tracebacks.
- If a tool output is huge, summarize it and preserve key lines (head/tail), plus any
  referenced file paths or IDs.

Required structure:
<state_snapshot>
  <goal>...</goal>
  <constraints>...</constraints>
  <key_facts>...</key_facts>
  <recent_actions>...</recent_actions>
  <current_state>...</current_state>
  <open_questions>...</open_questions>
  <next_steps>...</next_steps>
</state_snapshot>
"""


def _extract_tool_result_text(tool_result_content: Any) -> str:
    if tool_result_content is None:
        return ""
    if isinstance(tool_result_content, str):
        return tool_result_content
    if isinstance(tool_result_content, list):
        if not tool_result_content:
            return ""
        first = tool_result_content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            return str(first.get("text") or "")
        return str(first)
    return str(tool_result_content)


def build_anthropic_transcript(
    *,
    system: Any,
    messages: List[Dict[str, Any]],
    tool_result_max_chars_for_summary: int,
) -> str:
    chunks: List[str] = []

    if system:
        try:
            if isinstance(system, str):
                sys_text = system
            else:
                sys_text = json.dumps(system, ensure_ascii=False)
        except Exception:
            sys_text = str(system)
        if sys_text.strip():
            chunks.append("SYSTEM:\n" + sys_text.strip())

    for msg in messages or []:
        role = str((msg or {}).get("role") or "")
        content = (msg or {}).get("content")
        chunks.append(f"\n[{role.upper()}]")

        if isinstance(content, str):
            chunks.append(content)
            continue

        if not isinstance(content, list):
            chunks.append(str(content))
            continue

        for block in content:
            if not isinstance(block, dict):
                chunks.append(str(block))
                continue

            btype = block.get("type")
            if btype == "text":
                chunks.append(str(block.get("text") or ""))
            elif btype in ("thinking", "redacted_thinking"):
                thinking = block.get("thinking")
                if thinking is None:
                    thinking = block.get("data")
                if thinking is not None:
                    chunks.append("[THINKING]\n" + str(thinking))
            elif btype == "image":
                chunks.append("[IMAGE omitted]")
            elif btype == "tool_use":
                name = block.get("name")
                tid = block.get("id")
                args = block.get("input")
                try:
                    args_s = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args_s = str(args)
                chunks.append(f"[TOOL_USE name={name} id={tid}]\n{args_s}")
            elif btype == "tool_result":
                tid = block.get("tool_use_id")
                name = block.get("name")
                raw = _extract_tool_result_text(block.get("content"))
                compact = compact_tool_result(raw, max_chars=tool_result_max_chars_for_summary)
                chunks.append(f"[TOOL_RESULT name={name} id={tid}]\n{compact}")
            else:
                try:
                    chunks.append(json.dumps(block, ensure_ascii=False))
                except Exception:
                    chunks.append(str(block))

    transcript = "\n".join(chunks).strip() + "\n"
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = compact_tool_result(transcript, max_chars=MAX_TRANSCRIPT_CHARS)
    return transcript


def _content_has_function_response(content: Dict[str, Any]) -> bool:
    parts = (content or {}).get("parts") or []
    if not isinstance(parts, list):
        return False
    for p in parts:
        if isinstance(p, dict) and "functionResponse" in p:
            return True
    return False


def _content_has_function_call(content: Dict[str, Any]) -> bool:
    parts = (content or {}).get("parts") or []
    if not isinstance(parts, list):
        return False
    for p in parts:
        if isinstance(p, dict) and "functionCall" in p:
            return True
    return False


def select_tail_contents(contents: List[Dict[str, Any]], keep_last_messages: int) -> List[Dict[str, Any]]:
    if not contents:
        return []
    if keep_last_messages <= 0:
        return []

    start = max(0, len(contents) - keep_last_messages)
    tail = contents[start:]

    # Avoid starting the tail on a bare functionResponse (often means we cut a tool pair).
    if tail and _content_has_function_response(tail[0]) and start > 0:
        start = max(0, start - 1)
        tail = contents[start:]

    # Avoid ending with a bare functionCall (rare but can happen in malformed histories).
    if tail and _content_has_function_call(tail[-1]) and not _content_has_function_response(tail[-1]):
        tail = tail[:-1]

    return tail


def build_checkpoint_contents(summary_xml: str, tail_contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summary_text = (summary_xml or "").strip()
    if not summary_text:
        summary_text = "<state_snapshot></state_snapshot>"

    prefix = "Context checkpoint (auto-generated). Treat as authoritative history.\n\n"
    checkpoint_msg = {"role": "user", "parts": [{"text": prefix + summary_text}]}
    return [checkpoint_msg] + (tail_contents or [])


def parse_gemini_text_response(response_json: Dict[str, Any]) -> str:
    # Antigravity sometimes wraps in {"response": {...}}
    data = response_json.get("response") if isinstance(response_json, dict) else None
    if isinstance(data, dict):
        response_json = data

    candidates = (response_json or {}).get("candidates") or []
    if not candidates or not isinstance(candidates, list):
        return ""

    cand0 = candidates[0] or {}
    parts = (((cand0.get("content") or {}).get("parts")) or [])
    if not isinstance(parts, list):
        return ""

    out: List[str] = []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "".join(out).strip()


def _response_body_to_text(body: Any) -> str:
    if isinstance(body, memoryview):
        body = body.tobytes()
    if isinstance(body, (bytes, bytearray)):
        try:
            return body.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return str(body)


async def generate_state_snapshot_via_antigravity(
    *,
    transcript: str,
    summary_model: str,
    max_output_tokens: int,
    timeout_seconds: float = 120.0,
) -> Optional[str]:
    """Generate snapshot using Antigravity upstream (non-stream)."""
    from src.api.antigravity import non_stream_request

    request = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": transcript,
                    }
                ],
            }
        ],
        "systemInstruction": {"parts": [{"text": STATE_SNAPSHOT_SYSTEM_PROMPT}]},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": int(max_output_tokens),
            "topK": 32,
        },
    }

    body = {"model": summary_model, "request": request}

    try:
        resp = await non_stream_request(body=body)
        status = getattr(resp, "status_code", 500)
        if status != 200:
            try:
                raw = _response_body_to_text(getattr(resp, "body", b""))
            except Exception:
                raw = ""
            log.warning(
                f"[CTX-COMPRESS] Summary call failed (status={status}, model={summary_model}): {raw[:500]}"
            )
            return None

        raw_text = _response_body_to_text(getattr(resp, "body", b""))
        data = json.loads(raw_text) if raw_text else {}
        text = parse_gemini_text_response(data)
        return text or None
    except Exception as e:
        log.warning(f"[CTX-COMPRESS] Summary generation failed: {e}")
        return None


async def maybe_apply_checkpoint_to_gemini_request(
    *,
    gemini_request: Dict[str, Any],
    anthropic_request: Dict[str, Any],
    estimated_input_tokens: int,
    enabled: bool,
    trigger_input_tokens: int,
    keep_last_messages: int,
    summary_model: str,
    summary_max_output_tokens: int,
    tool_result_max_chars_for_summary: int,
) -> Tuple[Dict[str, Any], bool]:
    """Return (possibly modified request, applied?)."""
    if not enabled:
        return gemini_request, False
    if trigger_input_tokens <= 0:
        return gemini_request, False
    if estimated_input_tokens < trigger_input_tokens:
        return gemini_request, False

    contents = gemini_request.get("contents")
    if not isinstance(contents, list) or not contents:
        return gemini_request, False

    transcript = build_anthropic_transcript(
        system=anthropic_request.get("system"),
        messages=anthropic_request.get("messages") or [],
        tool_result_max_chars_for_summary=tool_result_max_chars_for_summary,
    )

    snapshot = await generate_state_snapshot_via_antigravity(
        transcript=transcript,
        summary_model=summary_model,
        max_output_tokens=summary_max_output_tokens,
    )
    if not snapshot:
        return gemini_request, False

    tail = select_tail_contents(contents, keep_last_messages)
    new_contents = build_checkpoint_contents(snapshot, tail)

    new_req = gemini_request.copy()
    new_req["contents"] = new_contents

    log.info(
        f"[CTX-COMPRESS] Applied checkpoint: est_tokens={estimated_input_tokens} -> messages={len(contents)} to {len(new_contents)}"
    )
    return new_req, True
