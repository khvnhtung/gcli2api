"""
Thinking Recovery Module

Handles corrupted conversation states when thinking blocks are missing or invalid.
Ported from antigravity-claude-proxy/src/format/thinking-utils.js.

Key scenarios handled:
1. Interrupted tool calls - user sends new message before tool_result
2. Tool loops with missing thinking - thinking blocks stripped by client
3. Cross-model signature mismatch - switching between Claude and Gemini
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from log import log

from src.converter.thoughtSignature_fix import (
    MIN_SIGNATURE_LENGTH,
    get_cached_signature_family,
)


# ============================================================================
# Helper Functions
# ============================================================================


def _is_thinking_part(block: Dict[str, Any]) -> bool:
    """
    Check if a block is a thinking block (any format).

    Supports:
    - Anthropic: {"type": "thinking", "thinking": "..."}
    - Anthropic: {"type": "redacted_thinking", "data": "..."}
    - Gemini: {"thought": true, "text": "..."}
    """
    if not isinstance(block, dict):
        return False

    block_type = block.get("type")
    if block_type in ("thinking", "redacted_thinking"):
        return True

    if block.get("thinking") is not None:
        return True

    if block.get("thought") is True:
        return True

    return False


def _has_valid_signature(block: Dict[str, Any]) -> bool:
    """
    Check if thinking block has valid signature (>= MIN_SIGNATURE_LENGTH chars).

    Checks both:
    - block.signature (Anthropic style)
    - block.thoughtSignature (Gemini style)
    """
    if not isinstance(block, dict):
        return False

    # Gemini style: thought=True with thoughtSignature
    if block.get("thought") is True:
        sig = block.get("thoughtSignature")
        return isinstance(sig, str) and len(sig) >= MIN_SIGNATURE_LENGTH

    # Anthropic style: type=thinking with signature
    sig = block.get("signature")
    if isinstance(sig, str) and len(sig) >= MIN_SIGNATURE_LENGTH:
        return True

    # Also check thoughtSignature for Anthropic blocks (cross-format)
    sig = block.get("thoughtSignature")
    if isinstance(sig, str) and len(sig) >= MIN_SIGNATURE_LENGTH:
        return True

    return False


def _message_has_valid_thinking(message: Dict[str, Any]) -> bool:
    """
    Check if message has any VALID (signed) thinking blocks.

    Only counts thinking blocks that have valid signatures, not unsigned ones
    that will be dropped later.
    """
    content = message.get("content") or message.get("parts") or []
    if not isinstance(content, list):
        return False

    for block in content:
        if not _is_thinking_part(block):
            continue
        if _has_valid_signature(block):
            return True

    return False


def _message_has_tool_use(message: Dict[str, Any]) -> bool:
    """Check if message has tool_use blocks."""
    content = message.get("content") or message.get("parts") or []
    if not isinstance(content, list):
        return False

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            return True
        if block.get("functionCall") is not None:
            return True

    return False


def _message_has_tool_result(message: Dict[str, Any]) -> bool:
    """Check if message has tool_result blocks."""
    content = message.get("content") or message.get("parts") or []
    if not isinstance(content, list):
        return False

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return True
        if block.get("functionResponse") is not None:
            return True

    return False


def _is_plain_user_message(message: Dict[str, Any]) -> bool:
    """
    Check if message is a plain user text message (not tool_result).
    """
    if message.get("role") != "user":
        return False

    content = message.get("content") or message.get("parts") or []

    # String content is plain user message
    if isinstance(content, str):
        return True

    if not isinstance(content, list):
        return False

    # Check if it has tool_result blocks
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return False
        if block.get("functionResponse") is not None:
            return False

    return True


# ============================================================================
# State Analysis
# ============================================================================


@dataclass
class ConversationState:
    """Represents the analyzed state of a conversation."""

    in_tool_loop: bool  # assistant has tool_use + tool_results after
    interrupted_tool: bool  # assistant has tool_use + plain user after (no tool_result)
    turn_has_thinking: bool  # last assistant has valid signed thinking
    tool_result_count: int  # number of tool_results after last assistant
    last_assistant_idx: int  # index of last assistant message (-1 if none)


def analyze_conversation_state(messages: List[Dict[str, Any]]) -> ConversationState:
    """
    Analyze conversation to detect if we're in a corrupted state.

    Detects:
    1. Tool loop: assistant has tool_use followed by tool_results (normal flow)
    2. Interrupted tool: assistant has tool_use followed by plain user message

    Args:
        messages: List of messages

    Returns:
        ConversationState with analysis results
    """
    if not isinstance(messages, list) or len(messages) == 0:
        return ConversationState(
            in_tool_loop=False,
            interrupted_tool=False,
            turn_has_thinking=False,
            tool_result_count=0,
            last_assistant_idx=-1,
        )

    # Find the last assistant message
    last_assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        role = messages[i].get("role", "")
        if role in ("assistant", "model"):
            last_assistant_idx = i
            break

    if last_assistant_idx == -1:
        return ConversationState(
            in_tool_loop=False,
            interrupted_tool=False,
            turn_has_thinking=False,
            tool_result_count=0,
            last_assistant_idx=-1,
        )

    last_assistant = messages[last_assistant_idx]
    has_tool_use = _message_has_tool_use(last_assistant)
    has_thinking = _message_has_valid_thinking(last_assistant)

    # Count trailing tool results and check for plain user messages
    tool_result_count = 0
    has_plain_user_after = False

    for i in range(last_assistant_idx + 1, len(messages)):
        msg = messages[i]
        if _message_has_tool_result(msg):
            tool_result_count += 1
        if _is_plain_user_message(msg):
            has_plain_user_after = True

    # We're in a tool loop if: assistant has tool_use AND there are tool_results after
    in_tool_loop = has_tool_use and tool_result_count > 0

    # We have an interrupted tool if: assistant has tool_use, NO tool_results,
    # but there IS a plain user message after (user interrupted and sent new message)
    interrupted_tool = has_tool_use and tool_result_count == 0 and has_plain_user_after

    return ConversationState(
        in_tool_loop=in_tool_loop,
        interrupted_tool=interrupted_tool,
        turn_has_thinking=has_thinking,
        tool_result_count=tool_result_count,
        last_assistant_idx=last_assistant_idx,
    )


# ============================================================================
# Recovery Detection
# ============================================================================


def has_gemini_history(messages: List[Dict[str, Any]]) -> bool:
    """
    Check if conversation history contains Gemini-style messages.

    Gemini puts thoughtSignature on tool_use blocks, Claude puts signature
    on thinking blocks. This detects Gemini→Claude cross-model scenarios.

    Args:
        messages: List of messages

    Returns:
        True if any tool_use has thoughtSignature (Gemini pattern)
    """
    for msg in messages:
        content = msg.get("content") or msg.get("parts") or []
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("thoughtSignature") is not None:
                return True

    return False


def has_unsigned_thinking_blocks(messages: List[Dict[str, Any]]) -> bool:
    """
    Check if conversation has unsigned thinking blocks that will be dropped.

    These cause "Expected thinking but found text" errors.

    Args:
        messages: List of messages

    Returns:
        True if any assistant message has unsigned thinking blocks
    """
    for msg in messages:
        role = msg.get("role", "")
        if role not in ("assistant", "model"):
            continue

        content = msg.get("content") or msg.get("parts") or []
        if not isinstance(content, list):
            continue

        for block in content:
            if _is_thinking_part(block) and not _has_valid_signature(block):
                return True

    return False


def needs_thinking_recovery(messages: List[Dict[str, Any]]) -> bool:
    """
    Check if conversation needs thinking recovery.

    Recovery is only needed when:
    1. We're in a tool loop or have an interrupted tool, AND
    2. No valid thinking blocks exist in the current turn

    Args:
        messages: List of messages

    Returns:
        True if thinking recovery is needed
    """
    state = analyze_conversation_state(messages)

    # Recovery is only needed in tool loops or interrupted tools
    if not state.in_tool_loop and not state.interrupted_tool:
        return False

    # Need recovery if no valid thinking blocks exist
    return not state.turn_has_thinking


# ============================================================================
# Recovery Actions
# ============================================================================


def strip_invalid_thinking_blocks(
    messages: List[Dict[str, Any]],
    target_family: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Strip invalid or incompatible thinking blocks from messages.

    Used before injecting synthetic messages for recovery.
    Keeps valid thinking blocks to preserve context from previous turns.

    Args:
        messages: List of messages
        target_family: Target model family ('claude' or 'gemini')
                      For Gemini: strict - drops unknown/mismatched signatures
                      For Claude: lenient - lets Claude validate its own

    Returns:
        New message list with invalid thinking blocks removed
    """
    stripped_count = 0
    result = []

    for msg in messages:
        content = msg.get("content")
        parts = msg.get("parts")

        # Determine which field to use
        if content is not None and isinstance(content, list):
            content_key = "content"
            content_list = content
        elif parts is not None and isinstance(parts, list):
            content_key = "parts"
            content_list = parts
        else:
            result.append(msg)
            continue

        filtered = []
        for block in content_list:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            # Keep non-thinking blocks
            if not _is_thinking_part(block):
                filtered.append(block)
                continue

            # Check generic validity (has signature of sufficient length)
            if not _has_valid_signature(block):
                stripped_count += 1
                continue

            # Check family compatibility only for Gemini targets
            # Claude can validate its own signatures, so we don't drop for Claude
            if target_family == "gemini":
                # Get signature based on block format
                if block.get("thought") is True:
                    sig = block.get("thoughtSignature")
                else:
                    sig = block.get("signature") or block.get("thoughtSignature")

                sig_family = get_cached_signature_family(sig) if sig else None

                # For Gemini: drop unknown or mismatched signatures
                if not sig_family or sig_family != target_family:
                    stripped_count += 1
                    continue

            filtered.append(block)

        # Use '.' instead of '' because claude models reject empty text parts
        if len(filtered) == 0:
            if content_key == "content":
                filtered = [{"type": "text", "text": "."}]
            else:
                filtered = [{"text": "."}]

        new_msg = {**msg, content_key: filtered}
        result.append(new_msg)

    if stripped_count > 0:
        log.debug(f"[ThinkingRecovery] Stripped {stripped_count} invalid/incompatible thinking block(s)")

    return result


def close_tool_loop_for_thinking(
    messages: List[Dict[str, Any]],
    target_family: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Close tool loop by injecting synthetic messages.

    This allows the model to start a fresh turn when thinking is corrupted.

    When thinking blocks are stripped (no valid signatures) and we're in the
    middle of a tool loop OR have an interrupted tool, the conversation is in
    a corrupted state. This function injects synthetic messages to close the
    loop and allow the model to continue.

    For interrupted tools:
        - Insert synthetic assistant message: "[Tool call was interrupted.]"
        - Insert at position after last assistant, before plain user message

    For tool loops:
        - Append synthetic assistant: "[Tool execution completed.]" or
          "[N tool executions completed.]"
        - Append synthetic user: "[Continue]"

    Args:
        messages: List of messages
        target_family: Target model family ('claude' or 'gemini')

    Returns:
        New message list with synthetic messages injected
    """
    state = analyze_conversation_state(messages)

    # Handle neither tool loop nor interrupted tool
    if not state.in_tool_loop and not state.interrupted_tool:
        return messages

    # Strip only invalid/incompatible thinking blocks (keep valid ones)
    modified = strip_invalid_thinking_blocks(messages, target_family)

    if state.interrupted_tool:
        # For interrupted tools: add a synthetic assistant message
        # to acknowledge the interruption before the user's new message

        # Find where to insert the synthetic message (after last assistant)
        insert_idx = state.last_assistant_idx + 1

        # Insert synthetic assistant message acknowledging interruption
        synthetic_msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "[Tool call was interrupted.]"}],
        }
        modified = modified[:insert_idx] + [synthetic_msg] + modified[insert_idx:]

        log.debug("[ThinkingRecovery] Applied thinking recovery for interrupted tool")

    elif state.in_tool_loop:
        # For tool loops: add synthetic messages to close the loop
        if state.tool_result_count == 1:
            synthetic_text = "[Tool execution completed.]"
        else:
            synthetic_text = f"[{state.tool_result_count} tool executions completed.]"

        # Inject synthetic model message to complete the turn
        modified.append({
            "role": "assistant",
            "content": [{"type": "text", "text": synthetic_text}],
        })

        # Inject synthetic user message to start fresh
        modified.append({
            "role": "user",
            "content": [{"type": "text", "text": "[Continue]"}],
        })

        log.debug("[ThinkingRecovery] Applied thinking recovery for tool loop")

    return modified


# ============================================================================
# Convenience Function
# ============================================================================


def apply_thinking_recovery_if_needed(
    messages: List[Dict[str, Any]],
    model_name: str,
    thinking_enabled: bool,
) -> List[Dict[str, Any]]:
    """
    Apply thinking recovery if needed based on model and conversation state.

    This is the main entry point for thinking recovery.

    Args:
        messages: List of messages
        model_name: Model name (e.g., "gemini-2.5-flash", "claude-sonnet-4-5")
        thinking_enabled: Whether thinking is enabled for this request

    Returns:
        Messages with recovery applied if needed, otherwise original messages
    """
    if not thinking_enabled:
        return messages

    if not needs_thinking_recovery(messages):
        return messages

    # Determine model family
    model_lower = model_name.lower()
    is_gemini = "gemini" in model_lower
    is_claude = "claude" in model_lower

    if is_gemini:
        log.debug("[ThinkingRecovery] Applying thinking recovery for Gemini")
        return close_tool_loop_for_thinking(messages, "gemini")

    if is_claude:
        # Claude needs recovery for cross-model or unsigned blocks
        needs_claude_recovery = has_gemini_history(messages) or has_unsigned_thinking_blocks(messages)
        if needs_claude_recovery:
            log.debug("[ThinkingRecovery] Applying thinking recovery for Claude")
            return close_tool_loop_for_thinking(messages, "claude")

    return messages
