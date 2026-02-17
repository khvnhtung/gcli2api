# OpenCode Compatibility Backlog for gcli2api

**Created**: 2026-02-18
**Context**: Analysis of [OpenCode](https://github.com/sst/opencode) source code vs gcli2api's Antigravity proxy to optimize Claude model family compatibility.
**OpenCode version analyzed**: Latest as of 2026-02-18 (AI SDK `ai@5.0.124`, `@ai-sdk/anthropic@2.0.62`)

---

## Phase 1 — Critical (Thinking Round-Trip is Broken)

These items directly cause thinking context loss between turns. Highest priority.

### 1.1 Emit `signature_delta` SSE Events for Thinking Blocks

**Impact**: CRITICAL — Without this, ALL thinking blocks are silently dropped on round-trip

**Problem**: The `@ai-sdk/anthropic` SDK expects thinking signatures delivered as `signature_delta` content block deltas:
```
event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig..."}}
```
gcli2api currently puts `thoughtSignature` on the `content_block_start` event (wrong location) using the wrong field name (`thoughtSignature` instead of `signature`). The SDK never receives a signature, so on the next request it drops unsigned thinking blocks entirely.

**Fix**:
- In `gemini_stream_to_anthropic_stream()` (`src/converter/anthropic2gemini.py` ~L1694-1745):
  - Remove `thoughtSignature` from `content_block_start` for thinking blocks
  - After all `thinking_delta` events for a block, emit a `signature_delta` event before `content_block_stop`
  - Use Anthropic field name `signature` everywhere in Anthropic output (not `thoughtSignature`)
- In `gemini_to_anthropic_response()` (~L1455-1471):
  - Change `block["thoughtSignature"]` to `block["signature"]` for thinking blocks

### 1.2 Accept `signature` Field on Incoming Thinking Blocks

**Impact**: CRITICAL — Complements 1.1; without this, round-tripped signatures from the SDK are ignored

**Problem**: When OpenCode sends thinking blocks back, the AI SDK uses `signature` (Anthropic standard). gcli2api only reads `thoughtSignature` (Gemini standard). So client-provided signatures are silently dropped, and `has_valid_thoughtsignature()` rejects valid blocks.

**Fix**:
- In `convert_messages_to_contents()` (~L965-1002): Also check `item.get("signature")` as fallback
- In `has_valid_thoughtsignature()` (`src/converter/thoughtSignature_fix.py`): Also check `block.get("signature")`
- In `filter_invalid_thinking_blocks()`: Same — check both field names
- Map incoming `signature` → `thoughtSignature` for the Gemini request

### 1.3 Stop Encoding Signatures into Tool IDs (Anthropic Output)

**Impact**: HIGH — Non-standard tool IDs; redundant when signatures flow through `signature_delta`

**Problem**: gcli2api encodes `thoughtSignature` into tool call IDs using `__thought__` separator (e.g., `toolu_abc__thought__eyJhbG...`). This was designed for Claude Code which strips custom fields. OpenCode's AI SDK properly handles `providerMetadata` for signature round-trips via `signature_delta`, so the ID encoding is unnecessary and creates non-standard tool IDs.

**Fix**:
- In streaming tool_use emission (~L1794-1863): Emit `original_id` directly, not `encoded_id`
- In non-streaming (~L1480-1503): Same
- **Keep** the `decode_tool_id_and_signature()` logic on the INPUT side for backward compatibility with existing sessions

---

## Phase 2 — High Priority (Correctness and Robustness)

### 2.1 Support `redacted_thinking` Block Type in Output

**Problem**: The AI SDK recognizes `redacted_thinking` as a distinct content block type with `data` field. gcli2api emits everything as `type: "thinking"`.

**Fix**: When a thinking part has opaque/encrypted content, emit as `{"type": "redacted_thinking", "data": "<base64>"}` in the Anthropic output.

### 2.2 Opus 4.6 Adaptive Thinking Effort Mapping

**Problem**: OpenCode sends `thinking: { type: "adaptive" }` with `effort: "low"|"medium"|"high"|"max"` for Opus 4.6. gcli2api maps `adaptive` to fixed 32000 tokens, ignoring the `effort` field.

**Fix** in `build_generation_config()` (`src/converter/anthropic2gemini.py` ~L1266):
```python
if thinking_config.get("type") == "adaptive":
    effort = thinking_config.get("effort", "high")
    effort_budgets = {"low": 4096, "medium": 16384, "high": 32000, "max": 48000}
    budget = effort_budgets.get(effort, 32000)
```

### 2.3 Context Overflow Error Message Normalization

**Problem**: OpenCode detects context overflow via regex patterns (`/prompt is too long/i`, `/exceeds the context window/i`). When upstream returns Google-style "too long" errors, gcli2api should normalize the message so OpenCode classifies it correctly.

**Fix**: In `gemini_chunk_wrapper` error handling, detect token/length errors from Google and include one of: `"prompt is too long"`, `"exceeds the context window"`.

---

## Phase 3 — Medium Priority (Polish and Edge Cases)

### 3.1 Add `retry-after-ms` Response Header

**Problem**: OpenCode's `SessionRetry.delay()` checks `retry-after-ms` (milliseconds) first, then `retry-after` (seconds). gcli2api only sets `Retry-After`.

**Fix**: In all 429 responses, add `retry-after-ms` header = `wait_seconds * 1000`.

### 3.2 Fix Error-Path Token Scaling Inconsistency

**Problem**: The error-path `message_start` event (~L1925) emits `input_tokens` without scaling, while all other paths scale it.

**Fix**: Apply `scale_usage_tokens()` in the error path too.

### 3.3 Fake Stream Signature Support

**Problem**: `build_anthropic_fake_stream_chunks()` completely drops signature data. Thinking blocks in fake-streamed responses will be unsigned and dropped on round-trip.

**Fix**: Accept optional signature data and emit `signature_delta` events after thinking deltas in the fake stream builder.

---

## Phase 4 — Low Priority (Documentation and Awareness)

### 4.1 Document OpenCode User-Agent Behavior

OpenCode does NOT send `User-Agent: opencode/x.x.x` for the `anthropic` provider. The AI SDK sends `ai-sdk/anthropic/2.0.62`. This means gcli2api's `opencode/` UA check for Gemini-3 search stripping won't match OpenCode. Currently OpenCode gets rerouted to GeminiCLI for web search (which works), so this may be the preferred behavior.

### 4.2 Cache Token Fields

OpenCode reads `cacheCreationInputTokens` from `providerMetadata.anthropic`. gcli2api doesn't emit these. Not impactful since Claude on Antigravity doesn't use prompt caching.

### 4.3 `ping` Event Format

Current format `event: ping\ndata: {"type":"ping"}\n\n` is already compatible. No change needed.

---

## Reference: Expected Anthropic SSE Sequence

```
# Thinking block:
event: content_block_start   → { type: "thinking", thinking: "" }
event: content_block_delta   → { type: "thinking_delta", thinking: "text..." }
event: content_block_delta   → { type: "signature_delta", signature: "sig..." }
event: content_block_stop    → { index: N }

# Text block:
event: content_block_start   → { type: "text", text: "" }
event: content_block_delta   → { type: "text_delta", text: "..." }
event: content_block_stop    → { index: N }

# Tool use block:
event: content_block_start   → { type: "tool_use", id: "toolu_xxx", name: "..." }
event: content_block_delta   → { type: "input_json_delta", partial_json: "..." }
event: content_block_stop    → { index: N }

# Redacted thinking:
event: content_block_start   → { type: "redacted_thinking", data: "<base64>" }
event: content_block_stop    → { index: N }

# Message envelope:
event: message_start → { message: { id, model, usage: { input_tokens } } }
event: message_delta → { delta: { stop_reason }, usage: { output_tokens } }
event: message_stop  → {}
```

**Signature round-trip in AI SDK**:
- Received: `providerMetadata.anthropic.signature` (from `signature_delta`)
- Stored: as `part.metadata` on reasoning parts
- Sent back: `providerOptions.anthropic.signature` → reconstructed as `{ type: "thinking", thinking: "...", signature: "..." }`
- Cross-model: stripped automatically when model changes
