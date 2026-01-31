"""
thoughtSignature 处理公共模块

提供统一的 thoughtSignature 编码/解码功能，用于在工具调用ID中保留签名信息。
这使得签名能够在客户端往返传输中保留，即使客户端会删除自定义字段。

Also provides:
- Signature caching for tool_use IDs (ported from antigravity-claude-proxy)
- Content reordering for proper message structure
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from log import log

# 在工具调用ID中嵌入thoughtSignature的分隔符
# 这使得签名能够在客户端往返传输中保留，即使客户端会删除自定义字段
THOUGHT_SIGNATURE_SEPARATOR = "__thought__"

# Signature cache configuration
SIGNATURE_CACHE_TTL_MS = 30 * 60 * 1000  # 30 minutes
MIN_SIGNATURE_LENGTH = 10  # Minimum valid signature length


def get_model_family(model_name: str) -> str:
    """
    Determine model family from model name.

    Args:
        model_name: Model name (e.g., "gemini-2.5-flash", "claude-sonnet-4-5")

    Returns:
        'claude' or 'gemini'
    """
    if not model_name:
        return "gemini"  # Default for Antigravity models

    lower = model_name.lower()
    if "claude" in lower:
        return "claude"
    return "gemini"


def encode_tool_id_with_signature(tool_id: str, signature: Optional[str]) -> str:
    """
    将 thoughtSignature 编码到工具调用ID中，以便往返保留。

    Args:
        tool_id: 原始工具调用ID
        signature: thoughtSignature（可选）

    Returns:
        编码后的工具调用ID

    Examples:
        >>> encode_tool_id_with_signature("call_123", "abc")
        'call_123__thought__abc'
        >>> encode_tool_id_with_signature("call_123", None)
        'call_123'
    """
    if not signature:
        return tool_id
    return f"{tool_id}{THOUGHT_SIGNATURE_SEPARATOR}{signature}"


def decode_tool_id_and_signature(encoded_id: str) -> Tuple[str, Optional[str]]:
    """
    从编码的ID中提取原始工具ID和thoughtSignature。

    Args:
        encoded_id: 编码的工具调用ID

    Returns:
        (原始工具ID, thoughtSignature) 元组

    Examples:
        >>> decode_tool_id_and_signature("call_123__thought__abc")
        ('call_123', 'abc')
        >>> decode_tool_id_and_signature("call_123")
        ('call_123', None)
    """
    if not encoded_id or THOUGHT_SIGNATURE_SEPARATOR not in encoded_id:
        return encoded_id, None
    parts = encoded_id.split(THOUGHT_SIGNATURE_SEPARATOR, 1)
    return parts[0], parts[1] if len(parts) == 2 else None


# ============================================================================
# Signature Cache (ported from antigravity-claude-proxy)
# ============================================================================
#
# In-memory cache for Gemini thoughtSignatures.
# Gemini models require thoughtSignature on tool calls, but Claude Code
# strips non-standard fields. This cache stores signatures by tool_use_id
# so they can be restored in subsequent requests.
# ============================================================================

# Cache storage: tool_use_id -> {"signature": str, "timestamp": float}
_signature_cache: Dict[str, Dict[str, Any]] = {}

# Thinking signature cache: signature -> {"model_family": str, "timestamp": float}
_thinking_signature_cache: Dict[str, Dict[str, Any]] = {}


def cache_signature(tool_use_id: str, signature: str) -> None:
    """
    Store a signature for a tool_use_id.

    Args:
        tool_use_id: The tool use ID
        signature: The thoughtSignature to cache
    """
    if not tool_use_id or not signature:
        return
    _signature_cache[tool_use_id] = {
        "signature": signature,
        "timestamp": time.time() * 1000  # ms
    }
    log.debug(f"[SignatureCache] Cached signature for tool_use_id={tool_use_id[:20]}...")


def get_cached_signature(tool_use_id: str) -> Optional[str]:
    """
    Get a cached signature for a tool_use_id.

    Args:
        tool_use_id: The tool use ID

    Returns:
        The cached signature or None if not found/expired
    """
    if not tool_use_id:
        return None

    entry = _signature_cache.get(tool_use_id)
    if not entry:
        return None

    # Check TTL
    now_ms = time.time() * 1000
    if now_ms - entry["timestamp"] > SIGNATURE_CACHE_TTL_MS:
        del _signature_cache[tool_use_id]
        return None

    return entry["signature"]


def cache_thinking_signature(signature: str, model_family: str) -> None:
    """
    Cache a thinking block signature with its model family.

    Args:
        signature: The thinking signature to cache
        model_family: The model family ('claude' or 'gemini')
    """
    if not signature or len(signature) < MIN_SIGNATURE_LENGTH:
        return
    _thinking_signature_cache[signature] = {
        "model_family": model_family,
        "timestamp": time.time() * 1000
    }


def get_cached_signature_family(signature: str) -> Optional[str]:
    """
    Get the cached model family for a thinking signature.

    Args:
        signature: The signature to look up

    Returns:
        'claude', 'gemini', or None if not found/expired
    """
    if not signature:
        return None

    entry = _thinking_signature_cache.get(signature)
    if not entry:
        return None

    # Check TTL
    now_ms = time.time() * 1000
    if now_ms - entry["timestamp"] > SIGNATURE_CACHE_TTL_MS:
        del _thinking_signature_cache[signature]
        return None

    return entry["model_family"]


def clear_signature_cache() -> None:
    """Clear all entries from the signature cache."""
    _signature_cache.clear()
    log.debug("[SignatureCache] Cleared signature cache")


def clear_thinking_signature_cache() -> None:
    """Clear all entries from the thinking signature cache."""
    _thinking_signature_cache.clear()
    log.debug("[SignatureCache] Cleared thinking signature cache")


def cleanup_expired_signatures() -> int:
    """
    Remove expired entries from both caches.

    Returns:
        Number of entries removed
    """
    now_ms = time.time() * 1000
    removed = 0

    # Clean signature cache
    expired_keys = [
        k for k, v in _signature_cache.items()
        if now_ms - v["timestamp"] > SIGNATURE_CACHE_TTL_MS
    ]
    for k in expired_keys:
        del _signature_cache[k]
        removed += 1

    # Clean thinking signature cache
    expired_thinking = [
        k for k, v in _thinking_signature_cache.items()
        if now_ms - v["timestamp"] > SIGNATURE_CACHE_TTL_MS
    ]
    for k in expired_thinking:
        del _thinking_signature_cache[k]
        removed += 1

    if removed > 0:
        log.debug(f"[SignatureCache] Cleaned up {removed} expired entries")

    return removed


# ============================================================================
# Content Reordering (ported from antigravity-claude-proxy)
# ============================================================================
#
# Reorder content so that:
# 1. Thinking blocks come first (required when thinking is enabled)
# 2. Text blocks come in the middle (filtering out empty/useless ones)
# 3. Tool_use blocks come at the end (required before tool_result)
# ============================================================================


def _sanitize_text_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitize a text block by removing extra fields like cache_control.
    Only keeps: type, text
    """
    if not block or block.get("type") != "text":
        return block
    return {"type": "text", "text": block.get("text", "")}


def _sanitize_tool_use_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitize a tool_use block by removing extra fields like cache_control.
    Only keeps: type, id, name, input, thoughtSignature (for Gemini)
    """
    if not block or block.get("type") != "tool_use":
        return block

    sanitized: Dict[str, Any] = {"type": "tool_use"}
    if block.get("id") is not None:
        sanitized["id"] = block["id"]
    if block.get("name") is not None:
        sanitized["name"] = block["name"]
    if block.get("input") is not None:
        sanitized["input"] = block["input"]
    # Preserve thoughtSignature for Gemini models
    if block.get("thoughtSignature") is not None:
        sanitized["thoughtSignature"] = block["thoughtSignature"]
    return sanitized


def _sanitize_thinking_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitize a thinking block by removing extra fields like cache_control.
    Only keeps: type, thinking, signature/thoughtSignature
    """
    if not block:
        return block

    block_type = block.get("type")
    if block_type not in ("thinking", "redacted_thinking"):
        return block

    sanitized: Dict[str, Any] = {"type": block_type}
    if block.get("thinking") is not None:
        sanitized["thinking"] = block["thinking"]
    if block.get("signature") is not None:
        sanitized["signature"] = block["signature"]
    if block.get("thoughtSignature") is not None:
        sanitized["thoughtSignature"] = block["thoughtSignature"]
    if block_type == "redacted_thinking" and block.get("data") is not None:
        sanitized["data"] = block["data"]
    return sanitized


def reorder_assistant_content(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Reorder content so that:
    1. Thinking blocks come first (required when thinking is enabled)
    2. Text blocks come in the middle (filtering out empty/useless ones)
    3. Tool_use blocks come at the end (required before tool_result)

    Also sanitizes blocks to remove extra fields like cache_control.

    Args:
        content: Array of content blocks

    Returns:
        Reordered and sanitized content array
    """
    if not isinstance(content, list):
        return content

    # Single element - just sanitize if needed
    if len(content) == 1:
        block = content[0]
        if block and block.get("type") in ("thinking", "redacted_thinking"):
            return [_sanitize_thinking_block(block)]
        return content

    thinking_blocks: List[Dict[str, Any]] = []
    text_blocks: List[Dict[str, Any]] = []
    tool_use_blocks: List[Dict[str, Any]] = []
    dropped_empty = 0

    for block in content:
        if not block:
            continue

        block_type = block.get("type")

        if block_type in ("thinking", "redacted_thinking"):
            thinking_blocks.append(_sanitize_thinking_block(block))
        elif block_type == "tool_use":
            tool_use_blocks.append(_sanitize_tool_use_block(block))
        elif block_type == "text":
            # Only keep text blocks with meaningful content
            text = block.get("text", "")
            if text and str(text).strip():
                text_blocks.append(_sanitize_text_block(block))
            else:
                dropped_empty += 1
        else:
            # Other block types go in the text position
            text_blocks.append(block)

    if dropped_empty > 0:
        log.debug(f"[ContentReorder] Dropped {dropped_empty} empty text block(s)")

    reordered = thinking_blocks + text_blocks + tool_use_blocks

    # Log only if actual reordering happened
    if len(reordered) == len(content):
        original_order = ",".join(b.get("type", "unknown") for b in content if b)
        new_order = ",".join(b.get("type", "unknown") for b in reordered if b)
        if original_order != new_order:
            log.debug("[ContentReorder] Reordered assistant content")

    return reordered


def reorder_messages_content(messages: List[Dict[str, Any]]) -> None:
    """
    Reorder content in all assistant/model messages.

    Args:
        messages: List of messages (modified in place)
    """
    reordered_count = 0

    for msg in messages:
        role = msg.get("role", "")
        if role not in ("assistant", "model"):
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        original_len = len(content)
        reordered = reorder_assistant_content(content)

        # Check if reordering actually changed something
        if reordered != content:
            msg["content"] = reordered
            reordered_count += 1

    if reordered_count > 0:
        log.debug(f"[ContentReorder] Reordered content in {reordered_count} message(s)")
