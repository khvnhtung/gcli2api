"""
Web Search Tool Handler

Intercepts web_search_20250305 server tool requests and executes them via
Gemini's googleSearch grounding, returning proper Anthropic server_tool_use +
web_search_tool_result content blocks.

Claude Code's WebSearch flow:
1. Claude Code makes a dedicated sub-request with web_search_20250305 in tools
2. The API is expected to return server_tool_use + web_search_tool_result blocks
3. Claude Code parses web_search_tool_result to count searches and extract URLs
4. Results are fed back to the main conversation

Our implementation:
1. Detect web_search_20250305 tool in request
2. Strip it from tools, send request to Gemini with googleSearch enabled
3. Extract groundingMetadata (URLs, titles) from Gemini response
4. Build proper server_tool_use + web_search_tool_result + text content blocks
5. Stream as Anthropic SSE events
"""

import json
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from log import log


# Default search model - fast and has googleSearch support
SEARCH_MODEL = "gemini-2.5-flash"


def has_web_search_tool(tools: Optional[List[Dict[str, Any]]]) -> bool:
    """Check if request contains web_search tool."""
    if not tools:
        return False
    for tool in tools:
        tool_type = tool.get("type", "")
        if isinstance(tool_type, str) and tool_type.startswith("web_search"):
            return True
    return False


def get_web_search_config(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract web_search configuration from tools."""
    for tool in tools:
        tool_type = tool.get("type", "")
        if isinstance(tool_type, str) and tool_type.startswith("web_search"):
            return {
                "max_uses": tool.get("max_uses", 5),
                "allowed_domains": tool.get("allowed_domains"),
                "blocked_domains": tool.get("blocked_domains"),
            }
    return {"max_uses": 5}


def strip_web_search_tool(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Remove web_search tool from tools list, keep other tools."""
    if not tools:
        return []
    return [t for t in tools if not (
        isinstance(t.get("type", ""), str) and t["type"].startswith("web_search")
    )]


def extract_grounding_results(
    gemini_response: Dict[str, Any],
) -> Tuple[str, List[Dict[str, str]], str]:
    """
    Extract grounding metadata from Gemini response.

    Returns:
        (query, search_results, text_content)
        - query: the search query used
        - search_results: list of {title, url} dicts
        - text_content: the model's text response
    """
    # Handle wrapped response format
    resp = gemini_response
    if "response" in resp:
        resp = resp["response"]

    candidates = resp.get("candidates", [])
    if not candidates:
        return "", [], ""

    candidate = candidates[0]

    # Extract text content
    parts = candidate.get("content", {}).get("parts", [])
    text_parts = []
    for part in parts:
        if isinstance(part, dict) and "text" in part:
            text_parts.append(part["text"])
    text_content = "\n".join(text_parts)

    # Extract grounding metadata
    grounding = candidate.get("groundingMetadata", {})

    # Get search query
    queries = grounding.get("webSearchQueries", [])
    query = queries[0] if queries else ""

    # Get search result URLs from groundingChunks
    search_results = []
    chunks = grounding.get("groundingChunks", [])
    for chunk in chunks:
        web = chunk.get("web", {})
        if web:
            search_results.append({
                "title": web.get("title", ""),
                "url": web.get("uri", ""),
            })

    log.info(
        f"[WEB_SEARCH] Extracted grounding: query={query!r}, "
        f"results={len(search_results)}, text={len(text_content)} chars"
    )

    return query, search_results, text_content


def build_web_search_content_blocks(
    query: str,
    search_results: List[Dict[str, str]],
    text_content: str,
) -> List[Dict[str, Any]]:
    """
    Build Anthropic content blocks with server_tool_use + web_search_tool_result.

    Claude Code expects:
    - server_tool_use: {type, id, name, input}
    - web_search_tool_result: {type, tool_use_id, content: [{title, url}]}
    - text: {type, text}
    """
    tool_use_id = f"srvtoolu_{uuid.uuid4().hex[:24]}"

    blocks = []

    # 1. server_tool_use block — the search invocation
    blocks.append({
        "type": "server_tool_use",
        "id": tool_use_id,
        "name": "web_search",
        "input": {"query": query},
    })

    # 2. web_search_tool_result block — the search results
    if search_results:
        blocks.append({
            "type": "web_search_tool_result",
            "tool_use_id": tool_use_id,
            "content": search_results,
        })
    else:
        # No results — return error format
        blocks.append({
            "type": "web_search_tool_result",
            "tool_use_id": tool_use_id,
            "content": {"error_code": "no_results"},
        })

    # 3. text block — the model's commentary
    if text_content:
        blocks.append({
            "type": "text",
            "text": text_content,
        })

    return blocks


def build_web_search_sse_events(
    content_blocks: List[Dict[str, Any]],
    model: str,
) -> List[Dict[str, Any]]:
    """
    Build Anthropic SSE streaming events for web search response.
    """
    message_id = f"msg_{uuid.uuid4().hex}"
    events = []

    # 1. message_start
    events.append({
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "server_tool_use": {"web_search_requests": 1},
            },
        },
    })

    # 2. Content blocks
    for idx, block in enumerate(content_blocks):
        block_type = block.get("type", "")

        if block_type == "server_tool_use":
            # content_block_start with server_tool_use
            events.append({
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "server_tool_use",
                    "id": block["id"],
                    "name": block["name"],
                    "input": {},
                },
            })
            # Send input via input_json_delta
            input_json = json.dumps(block.get("input", {}))
            events.append({
                "type": "content_block_delta",
                "index": idx,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": input_json,
                },
            })
            events.append({"type": "content_block_stop", "index": idx})

        elif block_type == "web_search_tool_result":
            # content_block_start with full result (arrives complete)
            events.append({
                "type": "content_block_start",
                "index": idx,
                "content_block": block,
            })
            events.append({"type": "content_block_stop", "index": idx})

        elif block_type == "text":
            events.append({
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            # Send text in chunks
            text = block.get("text", "")
            chunk_size = 100
            for i in range(0, max(len(text), 1), chunk_size):
                chunk = text[i:i + chunk_size]
                if chunk:
                    events.append({
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "text_delta", "text": chunk},
                    })
            events.append({"type": "content_block_stop", "index": idx})

    # 3. message_delta + message_stop
    events.append({
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 0},
    })
    events.append({"type": "message_stop"})

    return events


def build_web_search_non_stream_response(
    content_blocks: List[Dict[str, Any]],
    model: str,
) -> Dict[str, Any]:
    """Build non-streaming Anthropic response with web search results."""
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "server_tool_use": {"web_search_requests": 1},
        },
    }


async def execute_gemini_search(
    query: str,
    gemini_request_fn: Callable,
    max_retries: int = 2,
) -> Dict[str, Any]:
    """
    Execute search via Gemini with googleSearch grounding.

    Retries on empty grounding results (common with parallel requests
    hitting rate limits). Returns the raw Gemini response dict.
    """
    import asyncio

    log.info(f"[WEB_SEARCH] Executing Gemini search: {query}")

    gemini_request = {
        "model": SEARCH_MODEL,
        "request": {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": query}],
                }
            ],
            "tools": [{"googleSearch": {}}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 2048,
            },
        },
    }

    parsed: Dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        response = await gemini_request_fn(body=gemini_request)

        # Parse response
        if hasattr(response, "body"):
            body = response.body
            if isinstance(body, memoryview):
                body = body.tobytes()
            if isinstance(body, (bytes, bytearray)):
                body = body.decode("utf-8", errors="ignore")
            parsed = json.loads(body)
        else:
            parsed = response

        # Check if we got grounding results
        _, results, _ = extract_grounding_results(parsed)
        if results or attempt >= max_retries:
            if not results and attempt >= max_retries:
                log.warning(
                    f"[WEB_SEARCH] No grounding results after {max_retries + 1} attempts "
                    f"for query: {query[:80]}"
                )
            return parsed

        # Retry with backoff — likely rate-limited from parallel requests
        delay = 1.0 * (attempt + 1)
        log.info(
            f"[WEB_SEARCH] No grounding results on attempt {attempt + 1}, "
            f"retrying in {delay}s: {query[:80]}"
        )
        await asyncio.sleep(delay)

    return parsed


def is_claude_model(model: str) -> bool:
    """Check if model is a Claude model (needs web search interception)."""
    if not model:
        return False
    return "claude" in model.lower()
