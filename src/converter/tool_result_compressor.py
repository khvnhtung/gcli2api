"""
Tool Result Compressor Module

Provides intelligent compression for tool results to prevent "Prompt is too long" errors.

Strategies:
1. HTML deep cleaning (remove style, script, base64)
2. Browser snapshot compression (head + tail preservation)
3. "Saved to file" notice extraction
4. Safe truncation (avoid cutting mid-tag/mid-JSON)

Based on Antigravity-Manager's tool_result_compressor.rs implementation.
"""

import re
from typing import Optional

from log import log

# ============================================================================
# Constants (matching Antigravity Manager)
# ============================================================================

# Maximum tool result characters (~200K, prevents prompt overflow)
DEFAULT_MAX_TOOL_RESULT_CHARS = 200_000

# Browser snapshot detection threshold
SNAPSHOT_DETECTION_THRESHOLD = 20_000

# Browser snapshot max chars after compression
SNAPSHOT_MAX_CHARS = 16_000

# Browser snapshot head/tail ratio
SNAPSHOT_HEAD_RATIO = 0.7
SNAPSHOT_TAIL_RATIO = 0.3


# ============================================================================
# Main Compression Function
# ============================================================================

def compact_tool_result(text: str, max_chars: Optional[int] = None) -> str:
    """
    Smart compression of tool results.
    
    Applies multiple strategies in order:
    1. HTML deep cleaning (if HTML detected)
    2. "Saved to file" notice extraction
    3. Browser snapshot head+tail preservation
    4. Safe truncation as fallback
    
    Args:
        text: The tool result text to compress
        max_chars: Maximum characters allowed (default: 200,000)
    
    Returns:
        Compressed text, or original if under limit
    """
    if max_chars is None:
        max_chars = DEFAULT_MAX_TOOL_RESULT_CHARS
    
    if not text or len(text) <= max_chars:
        return text
    
    original_len = len(text)
    
    # 1. Deep clean HTML if detected
    if _is_html_content(text):
        text = deep_clean_html(text)
        if len(text) <= max_chars:
            log.info(
                f"[ToolCompressor] HTML cleaning reduced {original_len} -> {len(text)} chars"
            )
            return text
    
    # 2. Detect "saved to file" pattern (Claude Code large output)
    compacted = compact_saved_output_notice(text, max_chars)
    if compacted:
        log.info(
            f"[ToolCompressor] Detected saved output notice, "
            f"compacted {original_len} -> {len(compacted)} chars"
        )
        return compacted
    
    # 3. Detect browser snapshot
    if len(text) > SNAPSHOT_DETECTION_THRESHOLD:
        compacted = compact_browser_snapshot(text, max_chars)
        if compacted:
            log.info(
                f"[ToolCompressor] Browser snapshot compression, "
                f"compacted {original_len} -> {len(compacted)} chars"
            )
            return compacted
    
    # 4. Simple truncation with safe boundaries
    result = truncate_safe(text, max_chars)
    log.info(
        f"[ToolCompressor] Safe truncation, "
        f"compacted {original_len} -> {len(result)} chars"
    )
    return result


# ============================================================================
# HTML Cleaning
# ============================================================================

def _is_html_content(text: str) -> bool:
    """Check if text appears to be HTML content."""
    text_lower = text[:1000].lower()  # Only check beginning for performance
    return (
        "<html" in text_lower or
        "<body" in text_lower or
        "<!doctype" in text_lower or
        "<head" in text_lower
    )


def deep_clean_html(html: str) -> str:
    """
    Remove style, script, base64, and excessive whitespace from HTML.
    
    This significantly reduces HTML content size while preserving
    the meaningful text content.
    """
    result = html
    
    # 1. Remove <style>...</style> and contents
    result = re.sub(
        r'(?is)<style\b[^>]*>.*?</style>',
        '[style omitted]',
        result
    )
    
    # 2. Remove <script>...</script> and contents
    result = re.sub(
        r'(?is)<script\b[^>]*>.*?</script>',
        '[script omitted]',
        result
    )
    
    # 3. Remove <noscript>...</noscript> and contents
    result = re.sub(
        r'(?is)<noscript\b[^>]*>.*?</noscript>',
        '',
        result
    )
    
    # 4. Remove inline base64 data URIs (images, fonts, etc.)
    result = re.sub(
        r'data:[^;/]+/[^;]+;base64,[A-Za-z0-9+/=]+',
        '[base64 omitted]',
        result,
        flags=re.IGNORECASE
    )
    
    # 5. Remove SVG content (often large and not useful for LLM)
    result = re.sub(
        r'(?is)<svg\b[^>]*>.*?</svg>',
        '[svg omitted]',
        result
    )
    
    # 6. Remove HTML comments
    result = re.sub(r'<!--.*?-->', '', result, flags=re.DOTALL)
    
    # 7. Collapse multiple newlines/whitespace
    result = re.sub(r'\n\s*\n', '\n', result)
    result = re.sub(r'[ \t]+', ' ', result)
    
    return result.strip()


# ============================================================================
# Saved Output Notice Compression
# ============================================================================

def compact_saved_output_notice(text: str, max_chars: int) -> Optional[str]:
    """
    Compress "output saved to file" notices from Claude Code.
    
    Detects pattern: "result (N characters) exceeds maximum allowed tokens. 
                      Output saved to <path>"
    
    Strategy: Extract only the key information (path, char count)
    """
    # Pattern matching Claude Code's large output notice
    pattern = re.compile(
        r'(?i)result\s*\(\s*(?P<count>[\d,]+)\s*characters\s*\)\s*'
        r'exceeds\s+maximum\s+allowed\s+tokens\.\s*'
        r'Output\s+(?:has\s+been\s+)?saved\s+to\s+(?P<path>[^\r\n]+)',
        re.IGNORECASE
    )
    
    match = pattern.search(text)
    if not match:
        return None
    
    count = match.group('count')
    raw_path = match.group('path')
    
    # Clean file path (remove trailing punctuation)
    file_path = raw_path.strip().rstrip(')]."\',.')
    
    # Extract lines for context
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    
    # Find the notice line
    notice_line = None
    for line in lines:
        if 'exceeds maximum allowed tokens' in line.lower() and 'saved to' in line.lower():
            notice_line = line
            break
    
    if not notice_line:
        notice_line = (
            f"result ({count} characters) exceeds maximum allowed tokens. "
            f"Output has been saved to {file_path}"
        )
    
    # Find format/schema line if present
    format_line = None
    for line in lines:
        if (line.startswith('Format:') or 
            'JSON array with schema' in line or 
            line.lower().startswith('schema:')):
            format_line = line
            break
    
    # Build compact output
    compact_lines = [notice_line]
    if format_line and format_line not in compact_lines:
        compact_lines.append(format_line)
    compact_lines.append(
        f"[tool_result omitted to reduce prompt size; read file locally if needed: {file_path}]"
    )
    
    result = '\n'.join(compact_lines)
    return truncate_safe(result, max_chars)


# ============================================================================
# Browser Snapshot Compression
# ============================================================================

def compact_browser_snapshot(text: str, max_chars: int) -> Optional[str]:
    """
    Compress browser snapshots using head + tail preservation.
    
    Detection: "page snapshot", "页面快照", or many "ref=" references
    
    Strategy: Keep 70% from head, 30% from tail, omit middle
    (Based on research showing LLMs attend less to middle content)
    """
    # Detect if this is a browser snapshot
    is_snapshot = (
        'page snapshot' in text.lower() or
        '页面快照' in text or
        text.count('ref=') > 30 or
        text.count('[ref=') > 30
    )
    
    if not is_snapshot:
        return None
    
    desired_max = min(max_chars, SNAPSHOT_MAX_CHARS)
    if len(text) <= desired_max:
        return None
    
    # Build metadata header
    meta = f"[page snapshot summarized to reduce prompt size; original {len(text)} chars]"
    overhead = len(meta) + 200  # Buffer for formatting
    budget = desired_max - overhead
    
    if budget < 1000:
        return None
    
    # Calculate head and tail lengths
    head_len = min(int(budget * SNAPSHOT_HEAD_RATIO), 10_000)
    head_len = max(head_len, 500)  # Minimum head size
    tail_len = min(budget - head_len, 3_000)
    
    # Extract head and tail
    head = text[:head_len]
    tail = text[-tail_len:] if tail_len > 0 and len(text) > head_len else ""
    
    omitted = len(text) - head_len - (len(tail) if tail else 0)
    
    # Build summarized output
    if tail:
        summarized = (
            f"{meta}\n"
            f"---[HEAD]---\n{head}\n"
            f"---[...omitted {omitted} chars]---\n"
            f"---[TAIL]---\n{tail}"
        )
    else:
        summarized = (
            f"{meta}\n"
            f"---[HEAD]---\n{head}\n"
            f"---[...omitted {omitted} chars]---"
        )
    
    return truncate_safe(summarized, max_chars)


# ============================================================================
# Safe Truncation
# ============================================================================

def truncate_safe(text: str, max_chars: int) -> str:
    """
    Truncate text while avoiding cutting in the middle of:
    - HTML tags (< ... >)
    - JSON braces ({ ... })
    
    Adds a truncation notice at the end.
    """
    if len(text) <= max_chars:
        return text
    
    # Reserve space for truncation notice
    notice_reserve = 50
    effective_max = max_chars - notice_reserve
    
    if effective_max <= 0:
        return text[:max_chars]
    
    split_pos = effective_max
    sub = text[:effective_max]
    
    # Avoid cutting inside HTML tags
    last_open = sub.rfind('<')
    last_close = sub.rfind('>')
    if last_open != -1 and (last_close == -1 or last_open > last_close):
        # We're inside a tag, back up to before the tag
        split_pos = last_open
    
    # Avoid cutting inside JSON braces (if close to end)
    last_brace = sub.rfind('{')
    last_close_brace = sub.rfind('}')
    if last_brace != -1 and (last_close_brace == -1 or last_brace > last_close_brace):
        # We're inside a JSON object
        if effective_max - last_brace < 100:
            split_pos = min(split_pos, last_brace)
    
    # Ensure we don't go too far back
    split_pos = max(split_pos, effective_max - 200)
    
    truncated = text[:split_pos]
    omitted = len(text) - split_pos
    
    return f"{truncated}\n...[truncated {omitted} chars]"


# ============================================================================
# Utility: Estimate Token Count
# ============================================================================

def estimate_tokens(text: str) -> int:
    """
    Rough token estimation for mixed content.
    
    Heuristics:
    - ASCII: ~4 chars per token
    - CJK/Unicode: ~1.5 chars per token
    - Add 15% safety margin
    """
    if not text:
        return 0
    
    # Count ASCII vs non-ASCII characters
    ascii_count = sum(1 for c in text if ord(c) < 128)
    non_ascii_count = len(text) - ascii_count
    
    # Estimate tokens
    ascii_tokens = ascii_count / 4.0
    non_ascii_tokens = non_ascii_count / 1.5
    
    # Add 15% safety margin
    total = (ascii_tokens + non_ascii_tokens) * 1.15
    
    return int(total)
