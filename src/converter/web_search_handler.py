"""
Web Search Tool Handler

Intercepts web_search tool calls from Claude and executes them via Gemini's googleSearch.
This provides agentic web search for Claude models on Antigravity.

Flow:
1. Detect web_search tool in request
2. Convert to callable function tool for Claude
3. Send request to Claude
4. If Claude returns web_search tool_use → execute via Gemini with googleSearch
5. Inject tool_result with search results
6. Loop back until no more searches or max_iterations
"""

import json
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from log import log


# Default search model - fast and has googleSearch support
SEARCH_MODEL = "gemini-2.5-flash"

# Web search tool schema for Claude
WEB_SEARCH_FUNCTION_TOOL = {
    "name": "web_search",
    "description": (
        "Search the web for current information. Use this when you need up-to-date "
        "information that may not be in your training data, such as current events, "
        "weather, stock prices, recent news, or any time-sensitive information."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web"
            }
        },
        "required": ["query"]
    }
}


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


def convert_web_search_to_function_tool(
    tools: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """
    Convert web_search server tool to regular function tool.

    This allows Claude to call it as a regular tool, which we then intercept.
    """
    if not tools:
        return [WEB_SEARCH_FUNCTION_TOOL]

    result = []
    has_web_search = False

    for tool in tools:
        tool_type = tool.get("type", "")
        if isinstance(tool_type, str) and tool_type.startswith("web_search"):
            has_web_search = True
            # Replace with function tool version
            result.append(WEB_SEARCH_FUNCTION_TOOL)
        else:
            result.append(tool)

    if not has_web_search:
        result.append(WEB_SEARCH_FUNCTION_TOOL)

    return result


def extract_web_search_calls(
    response: Dict[str, Any]
) -> List[Tuple[str, str]]:
    """
    Extract web_search tool calls from Claude response.

    Returns:
        List of (tool_use_id, query) tuples
    """
    search_calls = []
    content = response.get("content", [])

    if not isinstance(content, list):
        return search_calls

    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")
        block_name = block.get("name")

        if block_type != "tool_use":
            continue
        if block_name != "web_search":
            continue

        tool_id = block.get("id", f"toolu_{uuid.uuid4().hex}")
        input_data = block.get("input", {})
        query = input_data.get("query", "")

        if query:
            search_calls.append((tool_id, query))
            log.info(f"[WEB_SEARCH] Detected search call: id={tool_id[:20]}..., query={query[:50]}...")

    return search_calls


async def execute_web_search(
    query: str,
    gemini_request_fn: Callable,
    allowed_domains: Optional[List[str]] = None,
    blocked_domains: Optional[List[str]] = None,
) -> str:
    """
    Execute web search via Gemini's googleSearch.

    Args:
        query: Search query
        gemini_request_fn: Async function to make Gemini API request
        allowed_domains: Optional list of allowed domains
        blocked_domains: Optional list of blocked domains

    Returns:
        Search results as formatted string
    """
    log.info(f"[WEB_SEARCH] Executing search: {query}")

    # Build Gemini request with googleSearch
    gemini_request = {
        "model": SEARCH_MODEL,
        "request": {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": f"Search the web and provide comprehensive information about: {query}"}]
                }
            ],
            "tools": [{"googleSearch": {}}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 2048,
            }
        }
    }

    try:
        # Make request to Gemini
        response = await gemini_request_fn(body=gemini_request)

        # Parse response
        if hasattr(response, "body"):
            body = response.body
            if isinstance(body, memoryview):
                body = body.tobytes()
            if isinstance(body, (bytes, bytearray)):
                body = body.decode("utf-8", errors="ignore")
            response_data = json.loads(body)
        else:
            response_data = response

        # Extract text from response
        # Handle wrapped response format
        if "response" in response_data:
            response_data = response_data["response"]

        candidates = response_data.get("candidates", [])
        if not candidates:
            log.warning("[WEB_SEARCH] No candidates in Gemini response")
            return f"Search completed but no results found for: {query}"

        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = []
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                text_parts.append(part["text"])

        result = "\n".join(text_parts) if text_parts else f"No detailed results for: {query}"
        log.info(f"[WEB_SEARCH] Got results: {len(result)} chars")
        return result

    except Exception as e:
        log.error(f"[WEB_SEARCH] Search failed: {e}")
        return f"Web search failed for '{query}': {str(e)}"


def build_tool_result_message(
    tool_use_id: str,
    result: str
) -> Dict[str, Any]:
    """Build a tool_result message block."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": result
    }


def inject_search_results(
    messages: List[Dict[str, Any]],
    assistant_response: Dict[str, Any],
    search_results: List[Tuple[str, str, str]]  # (tool_id, query, result)
) -> List[Dict[str, Any]]:
    """
    Inject search results into message history.

    Adds:
    1. Assistant message with the tool_use blocks
    2. User message with tool_result blocks
    """
    new_messages = list(messages)

    # Add assistant message with full content (including tool_use)
    assistant_content = assistant_response.get("content", [])
    new_messages.append({
        "role": "assistant",
        "content": assistant_content
    })

    # Add user message with tool results
    tool_results = []
    for tool_id, query, result in search_results:
        tool_results.append(build_tool_result_message(tool_id, result))

    new_messages.append({
        "role": "user",
        "content": tool_results
    })

    return new_messages


async def handle_web_search_loop(
    original_request: Dict[str, Any],
    claude_request_fn: Callable,
    gemini_request_fn: Callable,
    convert_request_fn: Callable,
    convert_response_fn: Callable,
    model: str,
    session_id: Optional[str] = None,
    message_count: int = 0,
) -> Dict[str, Any]:
    """
    Handle agentic web search loop.

    Args:
        original_request: Original Anthropic-format request dict
        claude_request_fn: Async function to make Claude API request (returns Response)
        gemini_request_fn: Async function to make Gemini API request (returns Response)
        convert_request_fn: Function to convert Anthropic request to Gemini format
        convert_response_fn: Function to convert Gemini response to Anthropic format
        model: Model name
        session_id: Session ID for caching
        message_count: Current message count

    Returns:
        Final Anthropic-format response
    """
    tools = original_request.get("tools", [])
    search_config = get_web_search_config(tools)
    max_iterations = search_config.get("max_uses", 5)
    allowed_domains = search_config.get("allowed_domains")
    blocked_domains = search_config.get("blocked_domains")

    # Convert web_search to function tool
    modified_tools = convert_web_search_to_function_tool(tools)

    # Working copy of messages
    messages = list(original_request.get("messages", []))

    iteration = 0
    final_response = None

    while iteration < max_iterations:
        iteration += 1
        log.info(f"[WEB_SEARCH] Loop iteration {iteration}/{max_iterations}")

        # Build request for this iteration
        current_request = dict(original_request)
        current_request["messages"] = messages
        current_request["tools"] = modified_tools
        current_request["stream"] = False  # Always non-stream for loop

        # Convert to Gemini format and make request
        gemini_request = await convert_request_fn(current_request, session_id=session_id)
        gemini_request["model"] = model

        # Normalize request
        from src.converter.gemini_fix import normalize_gemini_request
        gemini_request = await normalize_gemini_request(gemini_request, mode="antigravity")

        # Make request
        api_request = {
            "model": gemini_request.pop("model"),
            "request": gemini_request
        }

        response = await claude_request_fn(body=api_request)

        # Parse response
        if hasattr(response, "body"):
            body = response.body
            if isinstance(body, memoryview):
                body = body.tobytes()
            if isinstance(body, (bytes, bytearray)):
                body = body.decode("utf-8", errors="ignore")
            gemini_response = json.loads(body)
        else:
            gemini_response = response

        log.debug(f"[WEB_SEARCH] Raw Gemini response: {json.dumps(gemini_response, ensure_ascii=False)[:500]}...")

        status_code = getattr(response, "status_code", 200)

        # Convert to Anthropic format
        anthropic_response = convert_response_fn(
            gemini_response,
            model,
            status_code,
            session_id=session_id,
            message_count=message_count + len(messages)
        )

        log.debug(f"[WEB_SEARCH] Anthropic response: {json.dumps(anthropic_response, ensure_ascii=False)[:500]}...")

        # Check for web_search tool calls
        search_calls = extract_web_search_calls(anthropic_response)

        if not search_calls:
            # No more searches needed
            log.info(f"[WEB_SEARCH] No more search calls, returning final response")
            final_response = anthropic_response
            break

        # Execute searches
        search_results = []
        for tool_id, query in search_calls:
            result = await execute_web_search(
                query,
                gemini_request_fn,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
            )
            search_results.append((tool_id, query, result))

        # Inject results into messages
        messages = inject_search_results(messages, anthropic_response, search_results)
        log.info(f"[WEB_SEARCH] Injected {len(search_results)} search results, messages now: {len(messages)}")

    if final_response is None:
        log.warning(f"[WEB_SEARCH] Max iterations ({max_iterations}) reached")
        # Return the last response we got
        final_response = anthropic_response

    return final_response


def is_claude_model(model: str) -> bool:
    """Check if model is a Claude model (needs web search interception)."""
    if not model:
        return False
    lower = model.lower()
    return "claude" in lower
