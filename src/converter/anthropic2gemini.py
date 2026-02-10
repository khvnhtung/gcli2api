"""
Anthropic 到 Gemini 格式转换器

提供请求体、响应和流式转换的完整功能。
"""
from __future__ import annotations

import copy
import json
import os
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from log import log
from src.converter.utils import merge_system_messages
from src.token_estimator import scale_usage_tokens

from src.converter.thoughtSignature_fix import (
    encode_tool_id_with_signature,
    decode_tool_id_and_signature,
    reorder_messages_content,
    cache_signature,
    get_cached_signature,
    cache_thinking_signature,
    get_cached_signature_family,
    get_model_family,
    cache_session_signature,
    get_session_signature,
    MIN_SIGNATURE_LENGTH,
    MIN_SESSION_SIGNATURE_LENGTH,
)

from src.converter.thinking_recovery import (
    apply_thinking_recovery_if_needed,
)

DEFAULT_TEMPERATURE = 0.4
_DEBUG_TRUE = {"1", "true", "yes", "on"}

# ============================================================================
# Thinking 块验证和清理
# ============================================================================

# MIN_SIGNATURE_LENGTH is now imported from thoughtSignature_fix


def has_valid_thoughtsignature(block: Dict[str, Any]) -> bool:
    """
    检查 thinking 块是否有有效签名
    
    Args:
        block: content block 字典
        
    Returns:
        bool: 是否有有效签名
    """
    if not isinstance(block, dict):
        return True
    
    block_type = block.get("type")
    if block_type not in ("thinking", "redacted_thinking"):
        return True  # 非 thinking 块默认有效
    
    thinking = block.get("thinking", "")
    thoughtsignature = block.get("thoughtSignature")
    
    # 空 thinking + 任意 thoughtsignature = 有效 (trailing signature case)
    if not thinking and thoughtsignature is not None:
        return True
    
    # 有内容 + 足够长度的 thoughtsignature = 有效
    if thoughtsignature and isinstance(thoughtsignature, str) and len(thoughtsignature) >= MIN_SIGNATURE_LENGTH:
        return True
    
    return False


def sanitize_thinking_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """
    清理 thinking 块,只保留必要字段(移除 cache_control 等)
    
    Args:
        block: content block 字典
        
    Returns:
        清理后的 block 字典
    """
    if not isinstance(block, dict):
        return block
    
    block_type = block.get("type")
    if block_type not in ("thinking", "redacted_thinking"):
        return block
    
    # 重建块,移除额外字段
    sanitized: Dict[str, Any] = {
        "type": block_type,
        "thinking": block.get("thinking", "")
    }
    
    thoughtsignature = block.get("thoughtSignature")
    if thoughtsignature:
        sanitized["thoughtSignature"] = thoughtsignature
    
    return sanitized


def clean_cache_control(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Remove cache_control fields from all content blocks in messages.

    This is a critical fix for Claude Code CLI which sends cache_control
    fields that the Cloud Code API rejects with "Extra inputs are not permitted".

    Ported from antigravity-claude-proxy's thinking-utils.js.

    Args:
        messages: Array of messages in Anthropic format

    Returns:
        Messages with cache_control fields removed
    """
    if not isinstance(messages, list):
        return messages

    removed_count = 0
    cleaned_messages = []

    for message in messages:
        if not isinstance(message, dict):
            cleaned_messages.append(message)
            continue

        content = message.get("content")

        # Handle string content (no cache_control possible)
        if isinstance(content, str):
            cleaned_messages.append(message)
            continue

        # Handle non-list content
        if not isinstance(content, list):
            cleaned_messages.append(message)
            continue

        # Clean each block in content array
        cleaned_content = []
        for block in content:
            if not isinstance(block, dict):
                cleaned_content.append(block)
                continue

            # Check if cache_control exists
            if "cache_control" not in block:
                cleaned_content.append(block)
                continue

            # Create a copy without cache_control
            clean_block = {k: v for k, v in block.items() if k != "cache_control"}
            cleaned_content.append(clean_block)
            removed_count += 1

        # Create new message with cleaned content
        cleaned_message = {**message, "content": cleaned_content}
        cleaned_messages.append(cleaned_message)

    if removed_count > 0:
        log.debug(f"[CacheControl] Removed cache_control from {removed_count} block(s)")

    return cleaned_messages


def remove_trailing_unsigned_thinking(blocks: List[Dict[str, Any]]) -> None:
    """
    移除尾部的无签名 thinking 块

    Args:
        blocks: content blocks 列表 (会被修改)
    """
    if not blocks:
        return
    
    # 从后向前扫描
    end_index = len(blocks)
    for i in range(len(blocks) - 1, -1, -1):
        block = blocks[i]
        if not isinstance(block, dict):
            break
        
        block_type = block.get("type")
        if block_type in ("thinking", "redacted_thinking"):
            if not has_valid_thoughtsignature(block):
                end_index = i
            else:
                break  # 遇到有效签名的 thinking 块,停止
        else:
            break  # 遇到非 thinking 块,停止
    
    if end_index < len(blocks):
        removed = len(blocks) - end_index
        del blocks[end_index:]
        log.debug(f"Removed {removed} trailing unsigned thinking block(s)")


def filter_invalid_thinking_blocks(messages: List[Dict[str, Any]]) -> None:
    """
    过滤消息中的无效 thinking 块，并清理所有 thinking 块的额外字段（如 cache_control）

    Args:
        messages: Anthropic messages 列表 (会被修改)
    """
    total_filtered = 0

    for msg in messages:
        # 只处理 assistant 和 model 消息
        role = msg.get("role", "")
        if role not in ("assistant", "model"):
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        original_len = len(content)
        new_blocks: List[Dict[str, Any]] = []

        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue

            block_type = block.get("type")
            if block_type not in ("thinking", "redacted_thinking"):
                new_blocks.append(block)
                continue

            # 所有 thinking 块都需要清理（移除 cache_control 等额外字段）
            # 检查 thinking 块的有效性
            if has_valid_thoughtsignature(block):
                # 有效签名，清理后保留
                new_blocks.append(sanitize_thinking_block(block))
            else:
                # 无效签名，将内容转换为 text 块
                thinking_text = block.get("thinking", "")
                if thinking_text and str(thinking_text).strip():
                    log.info(
                        f"[Claude-Handler] Converting thinking block with invalid thoughtSignature to text. "
                        f"Content length: {len(thinking_text)} chars"
                    )
                    new_blocks.append({"type": "text", "text": thinking_text})
                else:
                    log.debug("[Claude-Handler] Dropping empty thinking block with invalid thoughtSignature")

        msg["content"] = new_blocks
        filtered_count = original_len - len(new_blocks)
        total_filtered += filtered_count

        # 如果过滤后为空,添加一个空文本块以保持消息有效
        if not new_blocks:
            msg["content"] = [{"type": "text", "text": ""}]

    if total_filtered > 0:
        log.debug(f"Filtered {total_filtered} invalid thinking block(s) from history")


# ============================================================================
# 请求验证和提取
# ============================================================================


def _anthropic_debug_enabled() -> bool:
    """检查是否启用 Anthropic 调试模式"""
    return str(os.getenv("ANTHROPIC_DEBUG", "true")).strip().lower() in _DEBUG_TRUE


def _is_non_whitespace_text(value: Any) -> bool:
    """
    判断文本是否包含"非空白"内容。

    说明：下游（Antigravity/Claude 兼容层）会对纯 text 内容块做校验：
    - text 不能为空字符串
    - text 不能仅由空白字符（空格/换行/制表等）组成
    """
    if value is None:
        return False
    try:
        return bool(str(value).strip())
    except Exception:
        return False


def _remove_nulls_for_tool_input(value: Any) -> Any:
    """
    递归移除 dict/list 中值为 null/None 的字段/元素。

    背景：Roo/Kilo 在 Anthropic native tool 路径下，若收到 tool_use.input 中包含 null，
    可能会把 null 当作真实入参执行（例如"在 null 中搜索"）。
    """
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in value.items():
            if v is None:
                continue
            cleaned[k] = _remove_nulls_for_tool_input(v)
        return cleaned

    if isinstance(value, list):
        cleaned_list = []
        for item in value:
            if item is None:
                continue
            cleaned_list.append(_remove_nulls_for_tool_input(item))
        return cleaned_list

    return value

# ============================================================================
# 2. JSON Schema 清理 (Enhanced - ported from Antigravity-Manager)
# ============================================================================
#
# This module handles JSON Schema cleaning for Gemini API compatibility.
# Key features:
# 1. $ref/$defs flattening - expand schema references
# 2. anyOf/oneOf merging - select best branch from union types
# 3. allOf merging - combine all schema fragments
# 4. Empty object injection - add placeholder for empty object types
# 5. Type normalization - handle ["string", "null"] -> "string"
# 6. Constraint migration - move validation rules to description
# ============================================================================


def _collect_all_defs(schema: Any, defs: Dict[str, Any]) -> None:
    """
    Recursively collect all $defs and definitions from any nesting level.
    MCP tools often define $defs at arbitrary depths, not just root level.

    Args:
        schema: The schema to scan
        defs: Dictionary to collect definitions into (mutated)
    """
    if not isinstance(schema, dict):
        return

    # Collect $defs at current level
    if "$defs" in schema and isinstance(schema["$defs"], dict):
        for k, v in schema["$defs"].items():
            if k not in defs:  # First definition wins
                defs[k] = v

    # Collect definitions (Draft-07 style)
    if "definitions" in schema and isinstance(schema["definitions"], dict):
        for k, v in schema["definitions"].items():
            if k not in defs:
                defs[k] = v

    # Recurse into all values
    for key, value in schema.items():
        if key not in ("$defs", "definitions"):
            if isinstance(value, dict):
                _collect_all_defs(value, defs)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _collect_all_defs(item, defs)


def _flatten_refs(schema: Dict[str, Any], defs: Dict[str, Any]) -> None:
    """
    Recursively expand $ref references using collected definitions.
    Unresolved refs are converted to string type with a hint.

    Args:
        schema: The schema to process (mutated)
        defs: Dictionary of collected definitions
    """
    if not isinstance(schema, dict):
        return

    # Handle $ref at current level
    if "$ref" in schema:
        ref_path = schema.pop("$ref")
        # Extract ref name (e.g., "#/$defs/MyType" -> "MyType")
        ref_name = ref_path.split("/")[-1] if "/" in ref_path else ref_path

        if ref_name in defs:
            # Merge definition into current schema
            def_schema = defs[ref_name]
            if isinstance(def_schema, dict):
                for k, v in def_schema.items():
                    if k not in schema:  # Don't overwrite existing
                        schema[k] = copy.deepcopy(v)
                # Recursively flatten any refs in merged content
                _flatten_refs(schema, defs)
        else:
            # Unresolved ref: convert to string type
            schema["type"] = "string"
            hint = f"(Unresolved $ref: {ref_path})"
            desc = schema.get("description", "")
            schema["description"] = f"{desc} {hint}".strip()
            log.debug(f"[SCHEMA FIX] Unresolved $ref converted to string: {ref_path}")

    # Recurse into children
    for key, value in list(schema.items()):
        if isinstance(value, dict):
            _flatten_refs(value, defs)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _flatten_refs(item, defs)


def _score_schema_option(schema: Dict[str, Any]) -> int:
    """
    Score a schema branch for anyOf/oneOf selection.
    Object (3) > Array (2) > Scalar (1) > Null (0)

    Args:
        schema: Schema branch to score

    Returns:
        Integer score (higher = more complex/preferred)
    """
    if not isinstance(schema, dict):
        return 0

    if schema.get("properties") or schema.get("type") == "object":
        return 3
    if schema.get("items") or schema.get("type") == "array":
        return 2

    type_val = schema.get("type")
    if isinstance(type_val, str) and type_val.lower() != "null":
        return 1

    return 0


def _extract_best_schema_from_union(
    union_array: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """
    Select the best non-null schema from anyOf/oneOf array.

    Args:
        union_array: List of schema branches

    Returns:
        Deep copy of the best branch, or None if empty
    """
    best_option = None
    best_score = -1

    for item in union_array:
        if isinstance(item, dict):
            score = _score_schema_option(item)
            if score > best_score:
                best_score = score
                best_option = item

    return copy.deepcopy(best_option) if best_option else None


def _merge_union_type(schema: Dict[str, Any], union_key: str) -> bool:
    """
    Merge anyOf/oneOf into the parent schema.

    Args:
        schema: Parent schema (mutated)
        union_key: Either "anyOf" or "oneOf"

    Returns:
        True if merge was performed
    """
    if union_key not in schema:
        return False

    union_array = schema.get(union_key)
    if not isinstance(union_array, list):
        return False

    best_branch = _extract_best_schema_from_union(union_array)
    if not best_branch:
        del schema[union_key]
        return True

    # Merge best branch into schema
    for k, v in best_branch.items():
        if k == "properties":
            if "properties" not in schema:
                schema["properties"] = {}
            if isinstance(v, dict):
                for pk, pv in v.items():
                    if pk not in schema["properties"]:
                        schema["properties"][pk] = pv
        elif k == "required":
            if "required" not in schema:
                schema["required"] = []
            if isinstance(v, list):
                for rv in v:
                    if rv not in schema["required"]:
                        schema["required"].append(rv)
        elif k not in schema:
            schema[k] = v

    del schema[union_key]
    return True


def _merge_all_of(schema: Dict[str, Any]) -> bool:
    """
    Merge allOf array into the parent schema.

    Args:
        schema: Parent schema (mutated)

    Returns:
        True if merge was performed
    """
    if "allOf" not in schema:
        return False

    all_of = schema.pop("allOf")
    if not isinstance(all_of, list):
        return True

    merged_properties: Dict[str, Any] = {}
    merged_required: Set[str] = set()

    for sub_schema in all_of:
        if not isinstance(sub_schema, dict):
            continue

        # Merge properties
        if "properties" in sub_schema and isinstance(sub_schema["properties"], dict):
            for k, v in sub_schema["properties"].items():
                if k not in merged_properties:
                    merged_properties[k] = v

        # Merge required
        if "required" in sub_schema and isinstance(sub_schema["required"], list):
            merged_required.update(sub_schema["required"])

        # Merge other fields (first wins)
        for k, v in sub_schema.items():
            if k not in ("properties", "required", "allOf") and k not in schema:
                schema[k] = v

    # Apply merged properties
    if merged_properties:
        if "properties" not in schema:
            schema["properties"] = {}
        for k, v in merged_properties.items():
            if k not in schema["properties"]:
                schema["properties"][k] = v

    # Apply merged required
    if merged_required:
        if "required" not in schema:
            schema["required"] = []
        for r in merged_required:
            if r not in schema["required"]:
                schema["required"].append(r)

    return True


def _fix_empty_object(schema: Dict[str, Any]) -> bool:
    """
    Fix empty object types that Gemini API rejects.
    Injects a minimal placeholder property.

    This is the critical fix for Notion MCP and similar tools that have
    object types without defined properties.

    Args:
        schema: Schema to fix (mutated)

    Returns:
        True if fix was applied
    """
    if schema.get("type") != "object":
        return False

    properties = schema.get("properties")
    has_valid_props = isinstance(properties, dict) and len(properties) > 0

    if has_valid_props:
        return False

    # Inject placeholder property (same approach as Antigravity-Manager)
    schema["properties"] = {
        "reason": {
            "type": "string",
            "description": "Reason for calling this tool"
        }
    }
    schema["required"] = ["reason"]

    log.debug("[SCHEMA FIX] Injected placeholder property for empty object")
    return True


def _clean_schema_recursive(schema: Any) -> bool:
    """
    Recursively clean schema node.

    Args:
        schema: Schema to clean (mutated)

    Returns:
        True if this schema is nullable
    """
    if not isinstance(schema, dict):
        return False

    is_nullable = False

    # Merge allOf first
    _merge_all_of(schema)

    # Recursively clean children first
    if "properties" in schema and isinstance(schema["properties"], dict):
        nullable_keys: Set[str] = set()
        for k, v in schema["properties"].items():
            if _clean_schema_recursive(v):
                nullable_keys.add(k)

        # Remove nullable fields from required
        if nullable_keys and "required" in schema:
            if isinstance(schema["required"], list):
                schema["required"] = [
                    r for r in schema["required"]
                    if r not in nullable_keys
                ]
                if not schema["required"]:
                    del schema["required"]

    if "items" in schema:
        _clean_schema_recursive(schema["items"])

    # Clean anyOf/oneOf branches before merging
    for union_key in ("anyOf", "oneOf"):
        if union_key in schema and isinstance(schema[union_key], list):
            for branch in schema[union_key]:
                _clean_schema_recursive(branch)

    # Merge anyOf/oneOf
    _merge_union_type(schema, "anyOf")
    _merge_union_type(schema, "oneOf")

    # Check if this looks like a schema node
    looks_like_schema = any(
        k in schema for k in ("type", "properties", "items", "enum", "anyOf", "oneOf", "allOf")
    )

    if looks_like_schema:
        # Migrate constraints to description
        constraints = [
            ("minLength", "minLen"), ("maxLength", "maxLen"),
            ("pattern", "pattern"), ("minimum", "min"), ("maximum", "max"),
            ("multipleOf", "multipleOf"), ("exclusiveMinimum", "exclMin"),
            ("exclusiveMaximum", "exclMax"), ("minItems", "minItems"),
            ("maxItems", "maxItems"), ("format", "format"),
        ]

        hints = []
        for field, label in constraints:
            if field in schema and schema[field] is not None:
                hints.append(f"{label}: {schema[field]}")

        if hints:
            suffix = f" [Constraint: {', '.join(hints)}]"
            desc = schema.get("description", "")
            if suffix not in desc:
                schema["description"] = f"{desc}{suffix}".strip()

        # Whitelist filtering - only keep Gemini-supported fields
        allowed_fields = {"type", "description", "properties", "required", "items", "enum", "title"}
        keys_to_remove = [k for k in schema.keys() if k not in allowed_fields]
        for k in keys_to_remove:
            del schema[k]

        # Handle type field
        if "type" in schema:
            type_val = schema["type"]
            selected_type = None

            if isinstance(type_val, str):
                lower = type_val.lower()
                if lower == "null":
                    is_nullable = True
                else:
                    selected_type = lower
            elif isinstance(type_val, list):
                for t in type_val:
                    if isinstance(t, str):
                        lower = t.lower()
                        if lower == "null":
                            is_nullable = True
                        elif selected_type is None:
                            selected_type = lower

            schema["type"] = selected_type or "string"

        # Add nullable hint to description
        if is_nullable:
            desc = schema.get("description", "")
            if "nullable" not in desc:
                schema["description"] = f"{desc} (nullable)".strip()

        # Fix empty objects (critical for Notion MCP)
        _fix_empty_object(schema)

        # Align required with actual properties
        if "required" in schema:
            if "properties" in schema and isinstance(schema["properties"], dict):
                valid_keys = set(schema["properties"].keys())
                schema["required"] = [
                    r for r in schema["required"]
                    if r in valid_keys
                ]
                if not schema["required"]:
                    del schema["required"]
            else:
                del schema["required"]

        # Ensure type is set if properties exist
        if "properties" in schema and "type" not in schema:
            schema["type"] = "object"

        # Convert enum values to strings
        if "enum" in schema and isinstance(schema["enum"], list):
            schema["enum"] = [
                str(v) if not isinstance(v, str) else v
                for v in schema["enum"]
            ]

    return is_nullable


def clean_json_schema(schema: Any) -> Any:
    """
    Clean JSON Schema for Gemini API compatibility.

    This is an enhanced version ported from Antigravity-Manager that handles:
    1. $ref/$defs flattening (expand references)
    2. anyOf/oneOf merging (select best branch)
    3. allOf merging (combine all branches)
    4. Empty object injection (add placeholder property)
    5. Type array normalization (["string", "null"] -> "string")
    6. Unsupported field removal (whitelist approach)
    7. Constraint migration to description

    Args:
        schema: JSON Schema to clean

    Returns:
        Cleaned schema compatible with Gemini API
    """
    if not isinstance(schema, dict):
        return schema

    # Make a deep copy to avoid mutating input
    schema = copy.deepcopy(schema)

    # Phase 1: Collect and flatten $refs
    all_defs: Dict[str, Any] = {}
    _collect_all_defs(schema, all_defs)

    # Remove $defs/definitions from root
    schema.pop("$defs", None)
    schema.pop("definitions", None)

    # Flatten all refs
    _flatten_refs(schema, all_defs)

    # Phase 2: Recursive cleaning
    _clean_schema_recursive(schema)

    return schema


# ============================================================================
# 4. Tools 转换
# ============================================================================

def convert_tools(anthropic_tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """
    将 Anthropic tools[] 转换为下游 tools（functionDeclarations）结构。

    注意: 所有函数声明必须合并到单个 functionDeclarations 数组中，
    因为 Gemini API 不支持多个非搜索工具对象在 tools 数组中。
    错误: "Multiple tools are supported only when they are all search tools."

    特殊处理: Anthropic 的 web_search 工具类型会映射到 Gemini 的 googleSearch。
    """
    if not anthropic_tools:
        return None

    log.debug(f"[TOOLS] Converting {len(anthropic_tools)} tools")

    # Collect all function declarations into a single array
    function_declarations: List[Dict[str, Any]] = []
    has_google_search = False

    for tool in anthropic_tools:
        tool_type = tool.get("type", "")
        tool_name = tool.get("name", "")
        log.debug(f"[TOOLS] Processing tool: type={tool_type}, name={tool_name}")

        # Anthropic web_search tool → Gemini googleSearch
        # Anthropic format: {"type": "web_search_20250305", "name": "web_search", ...}
        # Also check for name-based detection (some clients use name instead of type)
        WEB_SEARCH_PATTERNS = {"web_search", "google_search", "google_search_retrieval"}
        if tool_type.startswith("web_search") or tool_name in WEB_SEARCH_PATTERNS:
            has_google_search = True
            log.info(f"[TOOLS] Mapping Anthropic web_search tool to Gemini googleSearch (type={tool_type}, name={tool_name})")
            continue  # Skip adding to functionDeclarations

        name = tool.get("name", "nameless_function")
        description = tool.get("description", "")
        input_schema = tool.get("input_schema", {}) or {}
        parameters = clean_json_schema(input_schema)

        function_declarations.append(
            {
                "name": name,
                "description": description,
                "parameters": parameters,
            }
        )

    # Build result tools array
    result: List[Dict[str, Any]] = []

    # Add googleSearch if web_search was detected (with enhancedContent like Antigravity-Proxy)
    if has_google_search:
        result.append({
            "googleSearch": {
                "enhancedContent": {
                    "imageSearch": {
                        "maxResultCount": 5
                    }
                }
            }
        })
        log.info(f"[TOOLS] Added googleSearch with enhancedContent to tools array")

    # Add function declarations if any
    if function_declarations:
        result.append({"functionDeclarations": function_declarations})

    log.debug(f"[TOOLS] Final tools: has_google_search={has_google_search}, function_count={len(function_declarations)}")

    if not result:
        return None

    return result


# ============================================================================
# 5. Messages 转换
# ============================================================================

def _extract_tool_result_output(content: Any, max_chars: Optional[int] = None) -> str:
    """
    从 tool_result.content 中提取输出字符串。
    
    Args:
        content: tool_result 的 content 字段
        max_chars: 保留参数签名兼容（不再使用）
    
    Returns:
        输出字符串
    """
    # Extract raw output
    raw_output: str
    if isinstance(content, list):
        if not content:
            return ""
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            raw_output = str(first.get("text", ""))
        else:
            raw_output = str(first)
    elif content is None:
        return ""
    else:
        raw_output = str(content)
    
    # No compression/truncation here. Keep the full tool output.
    return raw_output


def convert_messages_to_contents(
    messages: List[Dict[str, Any]],
    *,
    include_thinking: bool = True,
    session_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    将 Anthropic messages[] 转换为下游 contents[]（role: user/model, parts: []）。

    Args:
        messages: Anthropic 格式的消息列表
        include_thinking: 是否包含 thinking 块
        session_id: Optional session ID for session-level signature fallback
    """
    contents: List[Dict[str, Any]] = []

    # 第一遍：构建 tool_use_id -> (name, thoughtsignature) 的映射
    # 注意：存储的是编码后的 ID（可能包含签名）
    tool_use_info: Dict[str, tuple[str, Optional[str]]] = {}
    for msg in messages:
        raw_content = msg.get("content", "")
        if isinstance(raw_content, list):
            for item in raw_content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    encoded_tool_id = item.get("id")
                    tool_name = item.get("name")
                    if encoded_tool_id and tool_name:
                        # 解码获取原始ID和签名
                        original_id, thoughtsignature = decode_tool_id_and_signature(encoded_tool_id)
                        # 存储映射：编码ID -> (name, thoughtsignature)
                        tool_use_info[str(encoded_tool_id)] = (tool_name, thoughtsignature)

    for msg in messages:
        role = msg.get("role", "user")
        
        # system 消息已经由 merge_system_messages 处理，这里跳过
        if role == "system":
            continue
        
        # 支持 'assistant' 和 'model' 角色（Google history usage）
        gemini_role = "model" if role in ("assistant", "model") else "user"
        raw_content = msg.get("content", "")

        parts: List[Dict[str, Any]] = []
        if isinstance(raw_content, str):
            if _is_non_whitespace_text(raw_content):
                parts = [{"text": str(raw_content)}]
        elif isinstance(raw_content, list):
            for item in raw_content:
                if not isinstance(item, dict):
                    if _is_non_whitespace_text(item):
                        parts.append({"text": str(item)})
                    continue

                item_type = item.get("type")
                if item_type == "thinking":
                    if not include_thinking:
                        continue

                    thinking_text = item.get("thinking", "")
                    if thinking_text is None:
                        thinking_text = ""
                    
                    part: Dict[str, Any] = {
                        "text": str(thinking_text),
                        "thought": True,
                    }
                    
                    # 如果有 thoughtsignature 则添加
                    thoughtsignature = item.get("thoughtSignature")
                    if thoughtsignature:
                        part["thoughtSignature"] = thoughtsignature
                    
                    parts.append(part)
                elif item_type == "redacted_thinking":
                    if not include_thinking:
                        continue

                    thinking_text = item.get("thinking")
                    if thinking_text is None:
                        thinking_text = item.get("data", "")
                    
                    part_dict: Dict[str, Any] = {
                        "text": str(thinking_text or ""),
                        "thought": True,
                    }
                    
                    # 如果有 thoughtsignature 则添加
                    thoughtsignature = item.get("thoughtSignature")
                    if thoughtsignature:
                        part_dict["thoughtSignature"] = thoughtsignature
                    
                    parts.append(part_dict)
                elif item_type == "text":
                    text = item.get("text", "")
                    if _is_non_whitespace_text(text):
                        parts.append({"text": str(text)})
                elif item_type == "image":
                    source = item.get("source", {}) or {}
                    if source.get("type") == "base64":
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": source.get("media_type", "image/png"),
                                    "data": source.get("data", ""),
                                }
                            }
                        )
                elif item_type == "tool_use":
                    encoded_id = item.get("id") or ""
                    original_id, thoughtsignature = decode_tool_id_and_signature(encoded_id)

                    log.info(f"[SIGNATURE_TRACE] tool_use INPUT: encoded_id={encoded_id[:50]}..., extracted_sig={'present('+str(len(thoughtsignature))+')' if thoughtsignature else 'None'}")

                    # [Phase 3] Try to restore signature from cache if not present
                    if not thoughtsignature or len(thoughtsignature) < MIN_SIGNATURE_LENGTH:
                        cached_sig = get_cached_signature(original_id)
                        if cached_sig:
                            thoughtsignature = cached_sig
                            log.info(f"[SIGNATURE_TRACE] RESTORED from tool cache for tool_use_id={original_id[:30]}..., sig_len={len(cached_sig)}")
                        elif session_id:
                            # Fallback to session cache (rewind-safe)
                            session_sig = get_session_signature(session_id)
                            if session_sig:
                                thoughtsignature = session_sig
                                log.info(f"[SIGNATURE_TRACE] RESTORED from session cache for session={session_id}, sig_len={len(session_sig)}")
                            else:
                                log.info(f"[SIGNATURE_TRACE] NO CACHE HIT (tool or session) for tool_use_id={original_id[:30]}...")
                        else:
                            log.info(f"[SIGNATURE_TRACE] NO CACHE HIT for tool_use_id={original_id[:30]}...")

                    fc_part: Dict[str, Any] = {
                        "functionCall": {
                            "id": original_id,  # 使用原始ID，不带签名
                            "name": item.get("name"),
                            "args": item.get("input", {}) or {},
                        }
                    }

                    # 如果提取到签名则添加，否则使用占位符以满足 Gemini API 要求
                    if thoughtsignature:
                        fc_part["thoughtSignature"] = thoughtsignature
                        log.info(f"[SIGNATURE_TRACE] tool_use OUTPUT: using real signature, len={len(thoughtsignature)}")
                    else:
                        fc_part["thoughtSignature"] = "skip_thought_signature_validator"
                        log.warning(f"[SIGNATURE_TRACE] tool_use OUTPUT: using PLACEHOLDER signature (skip_thought_signature_validator)")

                    parts.append(fc_part)
                elif item_type == "tool_result":
                    output = _extract_tool_result_output(item.get("content"))
                    encoded_tool_use_id = item.get("tool_use_id") or ""
                    
                    # 解码获取原始ID（functionResponse不需要签名）
                    original_tool_use_id, _ = decode_tool_id_and_signature(encoded_tool_use_id)

                    # 从 tool_result 获取 name，如果没有则从映射中查找
                    func_name = item.get("name")
                    if not func_name and encoded_tool_use_id:
                        # 使用编码ID查找映射
                        tool_info = tool_use_info.get(str(encoded_tool_use_id))
                        if tool_info:
                            func_name = tool_info[0]  # 获取 name
                    if not func_name:
                        func_name = "unknown_function"
                    
                    parts.append(
                        {
                            "functionResponse": {
                                "id": original_tool_use_id,  # 使用解码后的原始ID以匹配functionCall
                                "name": func_name,
                                "response": {"output": output},
                            }
                        }
                    )
                else:
                    parts.append({"text": json.dumps(item, ensure_ascii=False)})
        else:
            if _is_non_whitespace_text(raw_content):
                parts = [{"text": str(raw_content)}]

        if not parts:
            continue

        contents.append({"role": gemini_role, "parts": parts})

    return contents


def reorganize_tool_messages(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    重新组织消息，满足 tool_use/tool_result 约束。
    将连续的 functionCall 和对应的 functionResponse 批量处理，
    确保并发工具调用的响应在单个 contents 条目中。
    """
    tool_results: Dict[str, Dict[str, Any]] = {}

    for msg in contents:
        for part in msg.get("parts", []) or []:
            if isinstance(part, dict) and "functionResponse" in part:
                tool_id = (part.get("functionResponse") or {}).get("id")
                if tool_id:
                    tool_results[str(tool_id)] = part

    flattened: List[Dict[str, Any]] = []
    for msg in contents:
        role = msg.get("role")
        for part in msg.get("parts", []) or []:
            flattened.append({"role": role, "parts": [part]})

    new_contents: List[Dict[str, Any]] = []
    i = 0
    while i < len(flattened):
        msg = flattened[i]
        part = msg["parts"][0]

        if isinstance(part, dict) and "functionResponse" in part:
            i += 1
            continue

        if isinstance(part, dict) and "functionCall" in part:
            # 收集连续的 functionCall parts
            function_call_parts = []
            function_response_parts = []

            while i < len(flattened):
                current_part = flattened[i]["parts"][0]
                if isinstance(current_part, dict) and "functionCall" in current_part:
                    function_call_parts.append(current_part)
                    tool_id = (current_part.get("functionCall") or {}).get("id")
                    if tool_id is not None and str(tool_id) in tool_results:
                        function_response_parts.append(tool_results[str(tool_id)])
                    i += 1
                else:
                    break

            # 添加所有 functionCall 到一个 model 消息
            if function_call_parts:
                new_contents.append({"role": "model", "parts": function_call_parts})

            # 添加所有 functionResponse 到一个 user 消息
            if function_response_parts:
                new_contents.append({"role": "user", "parts": function_response_parts})

            continue

        new_contents.append(msg)
        i += 1

    return new_contents


# ============================================================================
# 7. Tool Choice 转换
# ============================================================================

def convert_tool_choice_to_tool_config(tool_choice: Any) -> Optional[Dict[str, Any]]:
    """
    将 Anthropic tool_choice 转换为 Gemini toolConfig

    Args:
        tool_choice: Anthropic 格式的 tool_choice
            - {"type": "auto"}: 模型自动决定是否使用工具
            - {"type": "any"}: 模型必须使用工具
            - {"type": "tool", "name": "tool_name"}: 模型必须使用指定工具

    Returns:
        Gemini 格式的 toolConfig，如果无效则返回 None
    """
    if not tool_choice:
        return None
    
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        
        if choice_type == "auto":
            return {"functionCallingConfig": {"mode": "AUTO"}}
        elif choice_type == "any":
            return {"functionCallingConfig": {"mode": "ANY"}}
        elif choice_type == "tool":
            tool_name = tool_choice.get("name")
            if tool_name:
                return {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [tool_name],
                    }
                }
    
    # 无效或不支持的 tool_choice，返回 None
    return None


# ============================================================================
# 8. Generation Config 构建
# ============================================================================

def build_generation_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    根据 Anthropic Messages 请求构造下游 generationConfig。

    Returns:
        generation_config: 生成配置字典
    """
    config: Dict[str, Any] = {
        "topP": 1,
        "candidateCount": 1,
        "stopSequences": [
            "<|user|>",
            "<|bot|>",
            "<|context_request|>",
            "<|endoftext|>",
            "<|end_of_turn|>",
        ],
    }

    temperature = payload.get("temperature", None)
    config["temperature"] = DEFAULT_TEMPERATURE if temperature is None else temperature

    top_p = payload.get("top_p", None)
    if top_p is not None:
        config["topP"] = top_p

    top_k = payload.get("top_k", None)
    if top_k is not None:
        config["topK"] = top_k

    max_tokens = payload.get("max_tokens")
    if max_tokens is not None:
        config["maxOutputTokens"] = max_tokens

    # 处理 extended thinking 参数
    thinking = payload.get("thinking")
    is_plan_mode = False
    if thinking and isinstance(thinking, dict):
        thinking_type = thinking.get("type")
        budget_tokens = thinking.get("budget_tokens")

        # Store original thinking config for downstream use (gemini_fix.py)
        config["_anthropic_thinking"] = thinking

        # "adaptive" is Claude Code's thinking mode for Opus 4.6 / Sonnet 4.
        # "enabled" is the explicit mode with fixed budget_tokens.
        if thinking_type in ("enabled", "adaptive"):
            is_plan_mode = thinking_type == "enabled"
            thinking_config: Dict[str, Any] = {}

            # CRITICAL FIX: Gemini API requires minimum 1024 tokens for thinking budget
            if budget_tokens is not None:
                effective_budget = max(1024, int(budget_tokens))
                if budget_tokens < 1024:
                    log.warning(f"[ANTHROPIC2GEMINI] budget_tokens {budget_tokens} below minimum 1024, using 1024")
                thinking_config["thinkingBudget"] = effective_budget
            elif thinking_type == "enabled":
                # Explicit enabled without budget — use large default
                thinking_config["thinkingBudget"] = 48000
            else:
                # Adaptive: Antigravity doesn't support true adaptive,
                # so we set a fixed budget matching Claude Code's default (31999)
                thinking_config["thinkingBudget"] = 32000

            thinking_config["includeThoughts"] = True

            config["thinkingConfig"] = thinking_config
            log.info(f"[ANTHROPIC2GEMINI] Thinking {thinking_type} with budget: {thinking_config['thinkingBudget']}")
        elif thinking_type == "disabled":
            # 明确禁用思考模式
            config["thinkingConfig"] = {
                "includeThoughts": False
            }
            log.info("[ANTHROPIC2GEMINI] Extended thinking explicitly disabled")

    # Effort level mapping (Claude API v2.0.67+)
    # Maps Claude's output_config.effort to Gemini's generationConfig.effortLevel
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        effort = output_config.get("effort")
        if isinstance(effort, str):
            effort_map = {"max": "HIGH", "high": "HIGH", "medium": "MEDIUM", "low": "LOW"}
            effort_level = effort_map.get(effort.lower(), "HIGH")
            config["effortLevel"] = effort_level
            log.info(f"[ANTHROPIC2GEMINI] Effort level: {effort} -> {effort_level}")

    stop_sequences = payload.get("stop_sequences")
    if isinstance(stop_sequences, list) and stop_sequences:
        config["stopSequences"] = config["stopSequences"] + [str(s) for s in stop_sequences]
    elif is_plan_mode:
        # Plan mode 时清空默认 stop sequences，避免过早停止
        # 默认的 stop sequences 可能会导致模型在生成计划时过早停止
        config["stopSequences"] = []
        log.info("[ANTHROPIC2GEMINI] Plan mode: cleared default stop sequences to prevent premature stopping")
    
    # 如果不是 plan mode 且没有自定义 stop_sequences，保持默认值
    # (默认值已经在 config 初始化时设置)

    return config


# ============================================================================
# 8. 主要转换函数
# ============================================================================

async def anthropic_to_gemini_request(
    payload: Dict[str, Any],
    session_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    将 Anthropic 格式请求体转换为 Gemini 格式请求体

    注意: 此函数只负责基础转换，不包含 normalize_gemini_request 中的处理
    (如 thinking config 自动设置、search tools、参数范围限制等)

    Args:
        payload: Anthropic 格式的请求体字典
        session_id: Optional session ID for session-level signature fallback

    Returns:
        Gemini 格式的请求体字典，包含:
        - contents: 转换后的消息内容
        - generationConfig: 生成配置
        - systemInstruction: 系统指令 (如果有)
        - tools: 工具定义 (如果有)
        - toolConfig: 工具调用配置 (如果有 tool_choice)
    """
    # 处理连续的system消息（兼容性模式）
    payload = await merge_system_messages(payload)

    # 提取和转换基础信息
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        messages = []

    # [CRITICAL FIX] 清理 cache_control 字段
    # Claude Code CLI 发送的 cache_control 会被 Cloud Code API 拒绝
    messages = clean_cache_control(messages)

    # [CRITICAL FIX] 重排序 assistant 消息内容
    # 确保 thinking 块在前，text 在中间，tool_use 在后
    reorder_messages_content(messages)

    # [CRITICAL FIX] 应用 Thinking 恢复（如果需要）
    # 处理中断的工具调用、工具循环、跨模型签名不兼容等情况
    model_name = payload.get("model", "")
    thinking_config = payload.get("thinking", {})
    thinking_enabled = thinking_config.get("type") == "enabled" if isinstance(thinking_config, dict) else False
    messages = apply_thinking_recovery_if_needed(messages, model_name, thinking_enabled)

    # [CRITICAL FIX] 过滤并修复 Thinking 块签名
    # 在转换前先过滤无效的 thinking 块
    filter_invalid_thinking_blocks(messages)

    # 构建生成配置
    generation_config = build_generation_config(payload)

    # 转换消息内容（始终包含thinking块，由响应端处理）
    contents = convert_messages_to_contents(messages, include_thinking=True, session_id=session_id)
    
    # [CRITICAL FIX] 移除尾部无签名的 thinking 块
    # 对真实请求应用额外的清理
    for content in contents:
        role = content.get("role", "")
        if role == "model":  # 只处理 model/assistant 消息
            parts = content.get("parts", [])
            if isinstance(parts, list):
                remove_trailing_unsigned_thinking(parts)
    
    contents = reorganize_tool_messages(contents)

    # 转换工具
    tools = convert_tools(payload.get("tools"))
    
    # 转换 tool_choice
    tool_config = convert_tool_choice_to_tool_config(payload.get("tool_choice"))

    # 构建基础请求数据
    gemini_request = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    
    # 如果 merge_system_messages 已经添加了 systemInstruction，使用它
    if "systemInstruction" in payload:
        gemini_request["systemInstruction"] = payload["systemInstruction"]
    
    if tools:
        gemini_request["tools"] = tools
    
    # 添加 toolConfig（如果有 tool_choice）
    if tool_config:
        gemini_request["toolConfig"] = tool_config

    return gemini_request


def gemini_to_anthropic_response(
    gemini_response: Dict[str, Any],
    model: str,
    status_code: int = 200,
    session_id: Optional[str] = None,
    message_count: Optional[int] = None
) -> Dict[str, Any]:
    """
    将 Gemini 格式非流式响应转换为 Anthropic 格式非流式响应

    注意: 如果收到的不是 200 开头的响应体，不做任何处理，直接转发

    Args:
        gemini_response: Gemini 格式的响应体字典
        model: 模型名称
        status_code: HTTP 状态码 (默认 200)
        session_id: Optional session ID for session-level signature caching
        message_count: Optional message count for rewind detection

    Returns:
        Anthropic 格式的响应体字典，或原始响应 (如果状态码不是 2xx)
    """
    # 非 2xx 状态码直接返回原始响应
    if not (200 <= status_code < 300):
        return gemini_response

    # 处理 GeminiCLI 的 response 包装格式
    if "response" in gemini_response:
        response_data = gemini_response["response"]
    else:
        response_data = gemini_response

    # 提取候选结果
    candidate = response_data.get("candidates", [{}])[0] or {}
    parts = candidate.get("content", {}).get("parts", []) or []

    # 获取 usage metadata
    usage_metadata = {}
    if "usageMetadata" in response_data:
        usage_metadata = response_data["usageMetadata"]
    elif "usageMetadata" in candidate:
        usage_metadata = candidate["usageMetadata"]

    # 转换内容块
    content = []
    has_tool_use = False

    for part in parts:
        if not isinstance(part, dict):
            continue

        # 处理 thinking 块
        if part.get("thought") is True:
            thinking_text = part.get("text", "")
            if thinking_text is None:
                thinking_text = ""

            block: Dict[str, Any] = {"type": "thinking", "thinking": str(thinking_text)}

            # 如果有 thoughtsignature 则添加
            thoughtsignature = part.get("thoughtSignature")
            if thoughtsignature:
                block["thoughtSignature"] = thoughtsignature
                # [Phase 3] Cache thinking signature with model family
                if len(thoughtsignature) >= MIN_SIGNATURE_LENGTH:
                    model_family = get_model_family(model)
                    cache_thinking_signature(thoughtsignature, model_family)

            content.append(block)
            continue

        # 处理文本块
        if "text" in part:
            content.append({"type": "text", "text": part.get("text", "")})
            continue

        # 处理工具调用
        if "functionCall" in part:
            has_tool_use = True
            fc = part.get("functionCall", {}) or {}
            original_id = fc.get("id") or f"toolu_{uuid.uuid4().hex}"
            thoughtsignature = part.get("thoughtSignature")

            # [Phase 3] Cache tool signature by tool_use_id for restoration on next request
            if thoughtsignature and len(thoughtsignature) >= MIN_SIGNATURE_LENGTH:
                cache_signature(original_id, thoughtsignature)

                # Also cache at session level for rewind detection
                if session_id and message_count and len(thoughtsignature) >= MIN_SESSION_SIGNATURE_LENGTH:
                    cache_session_signature(session_id, thoughtsignature, message_count)

            # 对工具调用ID进行签名编码
            encoded_id = encode_tool_id_with_signature(original_id, thoughtsignature)
            content.append(
                {
                    "type": "tool_use",
                    "id": encoded_id,
                    "name": fc.get("name") or "",
                    "input": _remove_nulls_for_tool_input(fc.get("args", {}) or {}),
                }
            )
            continue

        # 处理图片
        if "inlineData" in part:
            inline = part.get("inlineData", {}) or {}
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": inline.get("mimeType", "image/png"),
                        "data": inline.get("data", ""),
                    },
                }
            )
            continue

    # 确定停止原因
    finish_reason = candidate.get("finishReason")
    
    # 只有在正常停止（STOP）且有工具调用时才设为 tool_use
    # 避免在 SAFETY、MAX_TOKENS 等情况下仍然返回 tool_use 导致循环
    if has_tool_use and finish_reason == "STOP":
        stop_reason = "tool_use"
    elif finish_reason == "MAX_TOKENS":
        stop_reason = "max_tokens"
    else:
        # 其他情况（SAFETY、RECITATION 等）默认为 end_turn
        stop_reason = "end_turn"

    # 提取 token 使用情况
    input_tokens = usage_metadata.get("promptTokenCount", 0) if isinstance(usage_metadata, dict) else 0
    output_tokens = usage_metadata.get("candidatesTokenCount", 0) if isinstance(usage_metadata, dict) else 0

    # Scale Gemini token counts to fit Claude Code's 200K context window
    input_tokens, output_tokens = scale_usage_tokens(
        int(input_tokens or 0), int(output_tokens or 0), model
    )

    # 构建 Anthropic 响应
    message_id = f"msg_{uuid.uuid4().hex}"

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
        },
    }


async def gemini_stream_to_anthropic_stream(
    gemini_stream: AsyncIterator[bytes],
    model: str,
    status_code: int = 200,
    session_id: Optional[str] = None,
    message_count: Optional[int] = None
) -> AsyncIterator[bytes]:
    """
    将 Gemini 格式流式响应转换为 Anthropic SSE 格式流式响应

    注意: 如果收到的不是 200 开头的响应体，不做任何处理，直接转发

    Args:
        gemini_stream: Gemini 格式的流式响应 (bytes 迭代器)
        model: 模型名称
        status_code: HTTP 状态码 (默认 200)
        session_id: Optional session ID for session-level signature caching
        message_count: Optional message count for rewind detection

    Yields:
        Anthropic SSE 格式的响应块 (bytes)
    """
    # 非 2xx 状态码直接转发原始流
    if not (200 <= status_code < 300):
        async for chunk in gemini_stream:
            yield chunk
        return

    # 初始化状态
    message_id = f"msg_{uuid.uuid4().hex}"
    message_start_sent = False
    current_block_type: Optional[str] = None
    current_block_index = -1
    current_thinking_signature: Optional[str] = None
    has_tool_use = False
    input_tokens = 0
    output_tokens = 0
    finish_reason: Optional[str] = None

    def _sse_event(event: str, data: Dict[str, Any]) -> bytes:
        """生成 SSE 事件"""
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")

    def _close_block() -> Optional[bytes]:
        """关闭当前内容块"""
        nonlocal current_block_type
        if current_block_type is None:
            return None
        event = _sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": current_block_index},
        )
        current_block_type = None
        return event

    # 处理流式数据
    try:
        async for chunk in gemini_stream:
            # 记录接收到的原始chunk
            log.debug(f"[GEMINI_TO_ANTHROPIC] Raw chunk: {chunk[:200] if chunk else b''}")

            # 解析 Gemini 流式块
            if not chunk or not chunk.startswith(b"data: "):
                log.debug(f"[GEMINI_TO_ANTHROPIC] Skipping chunk (not SSE format or empty)")
                continue

            raw = chunk[6:].strip()
            if raw == b"[DONE]":
                log.debug(f"[GEMINI_TO_ANTHROPIC] Received [DONE] marker")
                break

            log.debug(f"[GEMINI_TO_ANTHROPIC] Parsing JSON: {raw[:200]}")

            try:
                data = json.loads(raw.decode('utf-8', errors='ignore'))
                log.debug(f"[GEMINI_TO_ANTHROPIC] Parsed data: {json.dumps(data, ensure_ascii=False)[:300]}")
            except Exception as e:
                log.warning(f"[GEMINI_TO_ANTHROPIC] JSON parse error: {e}")
                continue

            # Check if this is an Anthropic error event (from gemini_chunk_wrapper)
            # Format: {"type": "error", "error": {"type": "...", "message": "..."}}
            if data.get("type") == "error" and "error" in data:
                log.warning(f"[GEMINI_TO_ANTHROPIC] Received error event, emitting Anthropic error SSE: {data}")
                # Emit a proper Anthropic SSE error event.
                yield _sse_event("error", data)
                return

            # 处理 GeminiCLI 的 response 包装格式
            if "response" in data:
                response = data["response"]
            else:
                response = data

            candidate = (response.get("candidates", []) or [{}])[0] or {}
            parts = (candidate.get("content", {}) or {}).get("parts", []) or []

            # 更新 usage metadata
            if "usageMetadata" in response:
                usage = response["usageMetadata"]
                if isinstance(usage, dict):
                    if "promptTokenCount" in usage:
                        input_tokens = int(usage.get("promptTokenCount", 0) or 0)
                    if "candidatesTokenCount" in usage:
                        output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)

            # 发送 message_start（仅一次）
            if not message_start_sent:
                message_start_sent = True
                yield _sse_event(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": message_id,
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": scale_usage_tokens(input_tokens, output_tokens, model)[0], "output_tokens": output_tokens},
                        },
                    },
                )

            # 处理各种 parts
            for part in parts:
                if not isinstance(part, dict):
                    continue

                # 处理 thinking 块
                if part.get("thought") is True:
                    thinking_text = part.get("text", "")
                    thoughtsignature = part.get("thoughtSignature")

                    # [Phase 3] Cache thinking signature with model family for cross-model compatibility
                    if thoughtsignature and len(thoughtsignature) >= MIN_SIGNATURE_LENGTH:
                        model_family = get_model_family(model)
                        cache_thinking_signature(thoughtsignature, model_family)

                    # 检查是否需要关闭上一个块并开启新的 thinking 块
                    if current_block_type != "thinking":
                        close_evt = _close_block()
                        if close_evt:
                            yield close_evt

                        current_block_index += 1
                        current_block_type = "thinking"
                        current_thinking_signature = thoughtsignature

                        block: Dict[str, Any] = {"type": "thinking", "thinking": ""}
                        if thoughtsignature:
                            block["thoughtSignature"] = thoughtsignature
                        yield _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_block_index,
                                "content_block": block,
                            },
                        )
                    elif thoughtsignature and thoughtsignature != current_thinking_signature:
                        # 签名变化，需要开启新的 thinking 块
                        close_evt = _close_block()
                        if close_evt:
                            yield close_evt
                        
                        current_block_index += 1
                        current_block_type = "thinking"
                        current_thinking_signature = thoughtsignature
                        
                        block_new: Dict[str, Any] = {"type": "thinking", "thinking": ""}
                        if thoughtsignature:
                            block_new["thoughtSignature"] = thoughtsignature
                        
                        yield _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_block_index,
                                "content_block": block_new,
                            },
                        )

                    # 发送 thinking 文本增量
                    if thinking_text:
                        yield _sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": current_block_index,
                                "delta": {"type": "thinking_delta", "thinking": thinking_text},
                            },
                        )
                    continue

                # 处理文本块
                if "text" in part:
                    text = part.get("text", "")
                    if isinstance(text, str) and not text.strip():
                        continue

                    if current_block_type != "text":
                        close_evt = _close_block()
                        if close_evt:
                            yield close_evt

                        current_block_index += 1
                        current_block_type = "text"

                        yield _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_block_index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )

                    if text:
                        yield _sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": current_block_index,
                                "delta": {"type": "text_delta", "text": text},
                            },
                        )
                    continue

                # 处理工具调用
                if "functionCall" in part:
                    close_evt = _close_block()
                    if close_evt:
                        yield close_evt

                    has_tool_use = True
                    fc = part.get("functionCall", {}) or {}
                    original_id = fc.get("id") or f"toolu_{uuid.uuid4().hex}"
                    thoughtsignature = part.get("thoughtSignature")

                    log.info(f"[SIGNATURE_TRACE] RESPONSE functionCall: id={original_id[:30]}..., has_sig={thoughtsignature is not None}, sig_len={len(thoughtsignature) if thoughtsignature else 0}")

                    tool_id = encode_tool_id_with_signature(original_id, thoughtsignature)
                    tool_name = fc.get("name") or ""
                    tool_args = _remove_nulls_for_tool_input(fc.get("args", {}) or {})

                    # [Phase 3] Cache tool signature by tool_use_id for restoration on next request
                    if thoughtsignature and len(thoughtsignature) >= MIN_SIGNATURE_LENGTH:
                        cache_signature(original_id, thoughtsignature)
                        log.info(f"[SIGNATURE_TRACE] CACHING signature for future requests: id={original_id[:30]}...")

                        # Also cache at session level for rewind detection
                        if session_id and message_count and len(thoughtsignature) >= MIN_SESSION_SIGNATURE_LENGTH:
                            cache_session_signature(session_id, thoughtsignature, message_count)
                    else:
                        log.warning(f"[SIGNATURE_TRACE] NOT CACHING - no valid signature from upstream: id={original_id[:30]}...")

                    if _anthropic_debug_enabled():
                        log.info(
                            f"[ANTHROPIC][tool_use] 处理工具调用: name={tool_name}, "
                            f"id={tool_id}, has_signature={thoughtsignature is not None}"
                        )

                    current_block_index += 1
                    # 注意：工具调用不设置 current_block_type，因为它是独立完整的块

                    yield _sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": current_block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "input": {},
                            },
                        },
                    )

                    input_json = json.dumps(tool_args, ensure_ascii=False, separators=(",", ":"))
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": current_block_index,
                            "delta": {"type": "input_json_delta", "partial_json": input_json},
                        },
                    )

                    yield _sse_event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": current_block_index},
                    )
                    # 工具调用块已完全关闭，current_block_type 保持为 None
                    
                    if _anthropic_debug_enabled():
                        log.info(f"[ANTHROPIC][tool_use] 工具调用块已关闭: index={current_block_index}")
                    
                    continue

            # 检查是否结束
            if candidate.get("finishReason"):
                finish_reason = candidate.get("finishReason")
                break

        # 关闭最后的内容块
        close_evt = _close_block()
        if close_evt:
            yield close_evt

        # 确定停止原因
        # 只有在正常停止（STOP）且有工具调用时才设为 tool_use
        # 避免在 SAFETY、MAX_TOKENS 等情况下仍然返回 tool_use 导致循环
        if has_tool_use and finish_reason == "STOP":
            stop_reason = "tool_use"
        elif finish_reason == "MAX_TOKENS":
            stop_reason = "max_tokens"
        else:
            # 其他情况（SAFETY、RECITATION 等）默认为 end_turn
            stop_reason = "end_turn"

        if _anthropic_debug_enabled():
            log.info(
                f"[ANTHROPIC][stream_end] 流式结束: stop_reason={stop_reason}, "
                f"has_tool_use={has_tool_use}, finish_reason={finish_reason}, "
                f"input_tokens={input_tokens}, output_tokens={output_tokens}"
            )

        # 发送 message_delta 和 message_stop
        scaled_input, _ = scale_usage_tokens(input_tokens, output_tokens, model)
        yield _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {
                    "input_tokens": scaled_input,
                    "output_tokens": output_tokens,
                },
            },
        )

        yield _sse_event("message_stop", {"type": "message_stop"})

    except Exception as e:
        log.error(f"[ANTHROPIC] 流式转换失败: {e}")
        # 发送错误事件
        if not message_start_sent:
            yield _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                    },
                },
            )
        yield _sse_event(
            "error",
            {"type": "error", "error": {"type": "api_error", "message": str(e)}},
        )
