"""
Session Manager - Session ID extraction for signature caching

Ported from Antigravity-Manager/src-tauri/src/proxy/session_manager.rs

Provides stable session fingerprinting by hashing the first meaningful user message.
This ensures the same conversation uses the same session_id across turns,
maximizing prompt caching hit rate.
"""

import hashlib
from typing import Any, Dict, List


def extract_session_id(messages: List[Dict[str, Any]]) -> str:
    """
    Generate a stable session fingerprint by hashing the first meaningful user message.

    Design:
    - Only hash the first user message content (no model name or timestamp)
    - Ensures same conversation uses the same session_id across turns
    - Maximizes prompt caching hit rate

    Priority:
    1. First user message with len > 10 and no <system-reminder>
    2. Fallback: Hash the last message

    Args:
        messages: List of Anthropic-format messages

    Returns:
        Session ID in format "sid-{hash[:16]}"
    """
    hasher = hashlib.sha256()
    content_found = False

    for msg in messages:
        if msg.get("role") != "user":
            continue

        content = msg.get("content", "")
        text = ""

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Extract text from content blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            text = " ".join(text_parts)

        clean_text = text.strip()
        # Skip short messages or those with system reminders
        if len(clean_text) > 10 and "<system-reminder>" not in clean_text:
            hasher.update(clean_text.encode("utf-8"))
            content_found = True
            break  # Only use first meaningful message as anchor

    if not content_found:
        # Fallback: hash last message
        if messages:
            last_msg = messages[-1]
            content = last_msg.get("content", "")
            if isinstance(content, str):
                hasher.update(content.encode("utf-8"))
            else:
                hasher.update(str(content).encode("utf-8"))

    hash_hex = hasher.hexdigest()
    return f"sid-{hash_hex[:16]}"
