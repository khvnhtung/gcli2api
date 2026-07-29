"""Token estimation using Gemini's native countTokens API with local fallback."""
from __future__ import annotations

import base64
import struct
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

from log import log


# ==================== Usage scaling for Claude Code compatibility ====================

# Real context windows per model family.
# Claude models: 200K real, and current OpenCode clients compact correctly at that scale.
# Do not upscale Claude usage; upscaling caused repeated premature compaction in practice.
# Gemini models: 1M-2M real — scale DOWN to 200K for Claude Code.
_GEMINI_CONTEXT_LIMITS: Dict[str, int] = {
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-3-flash": 1_048_576,
    "gemini-3.1-pro": 2_097_152,
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
    """Scale token counts so client compaction triggers at the right fill level.

    Claude models → pass through unscaled.
    Gemini models (1M+ real, 200K client) → scale DOWN to fit 200K virtual window.
    """
    if "claude" in model.lower():
        return input_tokens, output_tokens

    # Gemini: real 1M+ but client expects 200K — scale DOWN
    context_limit = _get_gemini_context_limit(model)
    if context_limit is None or context_limit <= CLAUDE_CODE_CONTEXT_WINDOW:
        return input_tokens, output_tokens

    ratio = CLAUDE_CODE_CONTEXT_WINDOW / context_limit
    scaled_input = int(input_tokens * ratio)
    # Output tokens stay unscaled — they're small and don't drive compaction
    return scaled_input, output_tokens


# ==================== Image token estimation ====================

# Gemini image token constants
# See: https://ai.google.dev/gemini-api/docs/image-understanding
_TOKENS_PER_TILE = 258
_SMALL_IMAGE_THRESHOLD = 384  # Both dims <= this → single tile
_MIN_CROP_UNIT = 256
_MAX_CROP_UNIT = 768
_FALLBACK_IMAGE_TOKENS = 1000  # Conservative default when dimensions unknown

# Max base64 chars to decode for header parsing (~4KB decoded)
_HEADER_B64_CHARS = 5400


def _extract_image_dimensions(
    base64_data: str, mime_type: str = ""
) -> Optional[Tuple[int, int]]:
    """Extract (width, height) from base64 image data by reading header bytes.

    Only decodes the first ~4KB needed for header parsing.
    Returns (width, height) or None if format is unrecognized.
    """
    try:
        header_b64 = base64_data[:_HEADER_B64_CHARS]
        # Fix base64 padding
        padding = len(header_b64) % 4
        if padding:
            header_b64 += "=" * (4 - padding)
        raw = base64.b64decode(header_b64)

        if len(raw) < 8:
            return None

        # PNG: \x89PNG\r\n\x1a\n + IHDR chunk
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            width = struct.unpack(">I", raw[16:20])[0]
            height = struct.unpack(">I", raw[20:24])[0]
            return (width, height)

        # JPEG: \xFF\xD8
        if raw[:2] == b"\xff\xd8":
            return _parse_jpeg_dimensions(raw)

        # GIF: GIF87a or GIF89a
        if raw[:3] == b"GIF" and len(raw) >= 10:
            width = struct.unpack("<H", raw[6:8])[0]
            height = struct.unpack("<H", raw[8:10])[0]
            return (width, height)

        # WebP: RIFF....WEBP
        if raw[:4] == b"RIFF" and len(raw) >= 30 and raw[8:12] == b"WEBP":
            return _parse_webp_dimensions(raw)

        return None
    except Exception:
        return None


def _parse_jpeg_dimensions(raw: bytes) -> Optional[Tuple[int, int]]:
    """Scan JPEG data for SOF marker to extract dimensions."""
    i = 2
    while i < len(raw) - 9:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        # SOF0 (baseline), SOF1 (extended), SOF2 (progressive)
        if marker in (0xC0, 0xC1, 0xC2):
            height = struct.unpack(">H", raw[i + 5 : i + 7])[0]
            width = struct.unpack(">H", raw[i + 7 : i + 9])[0]
            return (width, height)
        # Skip variable-length segments (but not RST/SOI/EOI/standalone markers)
        if 0xC0 <= marker <= 0xFE and marker not in (
            0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0x00,
        ):
            if i + 3 < len(raw):
                seg_len = struct.unpack(">H", raw[i + 2 : i + 4])[0]
                i += 2 + seg_len
                continue
        i += 2
    return None


def _parse_webp_dimensions(raw: bytes) -> Optional[Tuple[int, int]]:
    """Extract dimensions from WebP data."""
    try:
        chunk_id = raw[12:16]
        if chunk_id == b"VP8 " and len(raw) >= 30:
            # Lossy VP8
            width = struct.unpack("<H", raw[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", raw[28:30])[0] & 0x3FFF
            return (width, height)
        elif chunk_id == b"VP8L" and len(raw) >= 25:
            # Lossless VP8L
            bits = struct.unpack("<I", raw[21:25])[0]
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return (width, height)
        elif chunk_id == b"VP8X" and len(raw) >= 30:
            # Extended VP8X
            width = struct.unpack("<I", raw[24:27] + b"\x00")[0] + 1
            height = struct.unpack("<I", raw[27:30] + b"\x00")[0] + 1
            return (width, height)
    except Exception:
        pass
    return None


def _calculate_image_tokens(width: int, height: int) -> int:
    """Calculate Gemini token cost using the crop-unit tiling formula.

    Based on Google's documented formula:
    1. crop_unit = clamp(floor(min(w,h) / 1.5), 256, 768)
    2. tiles = ceil(w / crop_unit) * ceil(h / crop_unit)
    3. tokens = tiles * 258
    """
    if width <= 0 or height <= 0:
        return _TOKENS_PER_TILE

    # Small images: single tile
    if width <= _SMALL_IMAGE_THRESHOLD and height <= _SMALL_IMAGE_THRESHOLD:
        return _TOKENS_PER_TILE

    # Crop-unit formula
    crop_unit_raw = int(min(width, height) / 1.5)
    crop_unit = max(_MIN_CROP_UNIT, min(_MAX_CROP_UNIT, crop_unit_raw))

    tiles_w = ceil(width / crop_unit)
    tiles_h = ceil(height / crop_unit)
    total_tiles = tiles_w * tiles_h

    return total_tiles * _TOKENS_PER_TILE


def _estimate_image_block_tokens(block: Dict[str, Any]) -> int:
    """Estimate Gemini token cost for a single image content block.

    Handles both Anthropic format (type=image, source.data) and
    Gemini format (inlineData.data). Falls back to data-size heuristic
    if dimensions can't be extracted from the header.
    """
    base64_data = ""
    mime_type = ""

    # Anthropic format: {"type": "image", "source": {"type": "base64", ...}}
    source = block.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        base64_data = source.get("data", "")
        mime_type = source.get("media_type", "")
    # Gemini format: {"inlineData": {"mimeType": "...", "data": "..."}}
    elif "inlineData" in block:
        inline = block.get("inlineData") or {}
        base64_data = inline.get("data", "")
        mime_type = inline.get("mimeType", "")

    if not base64_data:
        return _FALLBACK_IMAGE_TOKENS

    # Try to extract dimensions from the binary header
    dims = _extract_image_dimensions(base64_data, mime_type)
    if dims:
        width, height = dims
        tokens = _calculate_image_tokens(width, height)
        log.debug(f"[TOKEN_COUNT] Image {width}x{height} → {tokens} tokens ({dims_label(width, height)})")
        return tokens

    # Fallback: estimate from base64 data length
    return _estimate_tokens_from_data_size(len(base64_data))


def dims_label(w: int, h: int) -> str:
    """Human-readable label for common resolutions."""
    pixels = w * h
    if pixels <= 384 * 384:
        return "small"
    elif pixels <= 1280 * 720:
        return "720p"
    elif pixels <= 1920 * 1080:
        return "1080p"
    elif pixels <= 2560 * 1440:
        return "1440p"
    else:
        return "4K+"


def _estimate_tokens_from_data_size(base64_len: int) -> int:
    """Estimate image tokens from base64 data length when dimensions unavailable.

    Uses a size-based lookup since compression ratios vary too widely
    (0.1-50 bytes/pixel) for a reliable pixels-from-bytes heuristic.
    """
    if base64_len <= 0:
        return _FALLBACK_IMAGE_TOKENS

    # base64 → raw bytes: ~75%
    raw_bytes = base64_len * 3 // 4

    # Size-based estimation (conservative — prefer overcount to undercount)
    if raw_bytes < 50_000:
        # Small file: likely a small image or icon
        return _TOKENS_PER_TILE  # 258
    elif raw_bytes < 200_000:
        # Medium: typical screenshot or photo thumbnail
        # Assume ~720p equivalent → 6 tiles
        return 6 * _TOKENS_PER_TILE  # 1548
    elif raw_bytes < 500_000:
        # Large: high-quality screenshot or photo
        # Assume ~1080p equivalent → 6 tiles
        return 6 * _TOKENS_PER_TILE  # 1548
    elif raw_bytes < 2_000_000:
        # Very large: 1440p+ or uncompressed
        return 8 * _TOKENS_PER_TILE  # 2064
    else:
        # Huge: 4K or larger
        return 15 * _TOKENS_PER_TILE  # 3870


def _count_image_tokens_in_payload(payload: Dict[str, Any]) -> int:
    """Walk the payload and sum token costs for all image blocks."""
    total = 0
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 0

    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image" or "inlineData" in item:
                total += _estimate_image_block_tokens(item)

    return total


# ==================== Native countTokens via Gemini API ====================


def _anthropic_messages_to_gemini_contents(
    messages: List[Dict[str, Any]],
    skip_images: bool = False,
) -> List[Dict[str, Any]]:
    """Lightweight conversion of Anthropic messages to Gemini contents for counting only.

    When skip_images=True, image blocks are omitted (for hybrid counting where
    images are counted separately via dimension-based estimation).
    """
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
                    if skip_images:
                        continue
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
    For payloads with images, uses a hybrid approach: native count for text
    (with images stripped) + dimension-based image token estimation.
    """
    from config import get_code_assist_endpoint
    from src.credential_manager import credential_manager
    from src.httpx_client import post_async

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None

    has_images = _payload_has_images(payload)

    # Convert Anthropic messages → Gemini contents (skip images for native counting)
    try:
        contents = _anthropic_messages_to_gemini_contents(messages, skip_images=has_images)
    except Exception as e:
        log.debug(f"[TOKEN_COUNT] Failed to convert messages: {e}")
        return None

    if not contents:
        # If payload was only images with no text, just return image tokens
        if has_images:
            image_tokens = _count_image_tokens_in_payload(payload)
            extra = _estimate_system_and_tools(payload)
            total = image_tokens + extra
            log.debug(
                f"[TOKEN_COUNT] Images-only payload: image_tokens={image_tokens}, "
                f"system+tools={extra}, total={total}"
            )
            return total
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

            # For image payloads, add dimension-based image token estimate
            image_tokens = 0
            if has_images:
                image_tokens = _count_image_tokens_in_payload(payload)

            total = content_tokens + extra + image_tokens

            log.debug(
                f"[TOKEN_COUNT] Native hybrid: text={content_tokens}, "
                f"images={image_tokens}, system+tools={extra}, total={total}"
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
    """Local fallback: chars/4 for text, dimension-based cost per image.

    Skips base64 data fields to avoid inflating text counts.
    Uses image header parsing for accurate per-image token estimation.
    """
    total_chars = 0
    image_tokens = 0

    def _walk(obj: Any, inside_image: bool = False) -> None:
        nonlocal total_chars, image_tokens
        if isinstance(obj, str):
            if not inside_image:
                total_chars += len(obj)
        elif isinstance(obj, dict):
            is_image = obj.get("type") == "image" or "inlineData" in obj
            if is_image:
                image_tokens += _estimate_image_block_tokens(obj)
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
    return max(1, total_chars // 4 + image_tokens)
