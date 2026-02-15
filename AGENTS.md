# AGENTS.md - Coding Agent Guidelines for gcli2api

This document provides guidelines for AI coding agents working on the gcli2api codebase.

## Project Overview

gcli2api converts GeminiCLI and Antigravity to OpenAI, Gemini, and Claude API compatible interfaces.
It's a Python 3.12+ FastAPI application with async/await patterns throughout.

## Running the Service

### Systemd User Service (Recommended)

The service runs as a systemd user service using Python directly (not Docker):

```bash
# Reload systemd after changes to service file
systemctl --user daemon-reload

# Start/stop/restart the service
systemctl --user start gcli2api
systemctl --user stop gcli2api
systemctl --user restart gcli2api

# Check status
systemctl --user status gcli2api

# View logs
journalctl --user -u gcli2api -f        # Follow logs
journalctl --user -u gcli2api --no-pager -n 50  # Last 50 lines

# Enable on login
systemctl --user enable gcli2api
```

Service file location: `~/.config/systemd/user/gcli2api.service`

### Manual Execution

```bash
cd /home/lkt/gcli2api
.venv/bin/python web.py
```

## Credentials Storage

gcli2api stores its SQLite state at `./creds/credentials.db`.

Quick sanity check:
```bash
ls -la ./creds/credentials.db
```

## Build & Run Commands

```bash
# Install dependencies
make install           # Production dependencies
make install-dev       # Development dependencies (includes pytest, black, flake8, mypy)

# Run the application
make run               # Starts server on port 7861 (configurable via PORT env)
python web.py          # Direct execution

# Docker (optional, for deployment)
make docker-build      # Build Docker image
make docker-run        # Run container with default settings
docker build -t gcli2api:latest .
docker run -d --name gcli2api -p 7861:7861 -v ./creds:/app/creds gcli2api:latest
```

## Testing

```bash
# Run all tests
make test
python -m pytest -v

# Run a single test file
python -m pytest test/test_models.py -v

# Run a single test function
python -m pytest test/test_models.py::TestOpenAIModels::test_chat_message_basic -v

# Run tests matching a pattern
python -m pytest -k "test_retry" -v

# Run tests with coverage
make test-cov
python -m pytest --cov=src --cov-report=term-missing --cov-report=html
```

## Linting & Formatting

```bash
# Format code with black
make format
python -m black src/ web.py config.py log.py --line-length=100

# Run linters
make lint
python -m flake8 src/ web.py config.py log.py --max-line-length=100 --extend-ignore=E203,W503
python -m mypy src/ --ignore-missing-imports
```

## Code Style Guidelines

### Formatting
- **Line length**: 100 characters max
- **Indentation**: 4 spaces for Python
- **Quotes**: Double quotes preferred (black default)

### Imports
Order: 1) Standard library, 2) Third-party, 3) Local imports

```python
import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import Response
from pydantic import BaseModel

from log import log
from src.models import ChatCompletionRequest
```

### Type Hints
- Use type hints for all function parameters and return values
- Use `Optional[T]` for nullable types
- Use `Dict`, `List`, `Tuple` from typing module

```python
async def get_credential(mode: str = "geminicli") -> Optional[Tuple[str, Dict[str, Any]]]:
```

### Naming Conventions
- **Files**: snake_case (`retry_strategy.py`)
- **Classes**: PascalCase (`RetryStrategy`)
- **Functions**: snake_case (`determine_retry_strategy`)
- **Constants**: UPPER_SNAKE_CASE (`MAX_RETRIES`)
- **Private**: prefix with underscore (`_clean_schema_recursive`)

### Async/Await
This codebase is heavily async:

```python
async def fetch_data():
    await self._ensure_initialized()
    async with self._operation_lock:
        result = await self._storage_adapter.get_credential(name)
    return result
```

### Error Handling
- Log errors with context: `log.error(f"Operation failed for {name}: {e}")`
- Return `None` or empty collections on failure for internal APIs
- Use `create_error_response()` for HTTP error responses

### Logging
Use the custom `log` module:

```python
from log import log
log.debug("Detailed info")
log.info("General info")
log.warning("Warning")
log.error("Error occurred")
```

## Key Modules

### Retry Strategy (`src/api/retry_strategy.py`)
Handles intelligent retry with smart backoff by error type:

**Error Classification (RateLimitReason):**
- `QUOTA_EXHAUSTED`: Daily/hourly quota used up → Progressive [60s, 5m, 30m, 2h], rotate account
- `MODEL_CAPACITY_EXHAUSTED`: Google infrastructure overloaded → Short delays [5s-60s], same account
- `RATE_LIMIT_EXCEEDED`: Per-minute rate limit → Standard 30s backoff
- `SERVER_ERROR`: 5xx errors → Exponential backoff

**Account Rotation:**
- 429 QUOTA_EXHAUSTED: Rotate (quota is per-account)
- 429 MODEL_CAPACITY_EXHAUSTED: Don't rotate (Google's issue)
- 503/529: Don't rotate (global capacity issue)
- 401/403: Rotate (auth issue)

### Thinking Recovery (`src/converter/thinking_recovery.py`)
Handles corrupted conversation states:
- Interrupted tool calls → Injects `[Tool call was interrupted.]`
- Tool loops with missing thinking → Injects `[Tool execution completed.]` + `[Continue]`
- Cross-model signature mismatch → Strips incompatible signatures

### Signature Caching (`src/converter/thoughtSignature_fix.py`)
- Caches thoughtSignatures by tool_use_id with TTL
- Tracks model family ('claude' or 'gemini') for cross-model compatibility
- Content reordering: thinking → text → tool_use

### Structured Errors (`src/errors.py`)
Custom error classes for better error handling:
- `RateLimitError`, `CapacityExhaustedError`, `AuthError`
- Helper functions: `is_rate_limit_error()`, `classify_http_error()`

### JSON Schema Cleaning (`src/converter/anthropic2gemini.py`)
Cleans MCP tool schemas for Gemini API compatibility:
- `$ref`/`$defs` flattening
- `anyOf`/`oneOf` merging
- Empty object injection (fixes Notion MCP)
- Cache control stripping
- `WEB_SEARCH_PATTERNS`: Module-level constant of tool names mapped to googleSearch (importable)

### Converters (`src/converter/`)
- `anthropic2gemini.py`: Anthropic ↔ Gemini format conversion
- `openai2gemini.py`: OpenAI ↔ Gemini format conversion

### Routers (`src/router/`)
- `antigravity/`: Antigravity API endpoints (Claude/OpenAI compatible)
- `geminicli/`: GeminiCLI API endpoints

## Common Patterns

### Configuration Access
```python
from config import get_api_password, get_retry_429_enabled
password = await get_api_password()
```

### Credential Management
```python
from src.credential_manager import credential_manager
filename, cred_data = await credential_manager.get_valid_credential(mode="antigravity")
```

### Creating Error Responses
```python
from src.router.base_router import create_error_response
return create_error_response("Not found", status_code=404)
```

## Important Notes

1. **Streaming**: Most API calls use streaming. Errors in streams must be properly formatted as SSE events.

2. **Error Format**: Convert Google errors to Anthropic format for OpenCode compatibility:
   ```python
   {"type": "error", "error": {"type": "invalid_request_error", "message": "..."}}
   ```

3. **MCP Tools**: Tool schemas from MCP servers may have complex nested structures. The `clean_json_schema()` function handles these.

4. **Smart Backoff**: The retry strategy distinguishes between quota exhaustion (rotate account) and capacity exhaustion (retry same account).

5. **Thinking Recovery**: When thinking blocks are corrupted or missing, synthetic messages are injected to close tool loops.

6. **Service Restart**: After code changes, restart the service:
   ```bash
   systemctl --user restart gcli2api
   ```

7. **Search Handling on Antigravity**: `gemini-2.5-flash` supports native search. For `gemini-3*`, behavior is client-aware in `src/router/antigravity/anthropic.py`: OpenCode user agents (`opencode/...`) have `web_search` stripped to keep Antigravity routing stable, while non-OpenCode clients are rerouted to GeminiCLI Anthropic route where search works. Claude models are intercepted earlier by `web_search_handler.py`.

## Debugging Guide

### Terminology

| Term | Description |
|------|-------------|
| **Antigravity** | Google's internal API that routes Claude models through Vertex AI infrastructure. Uses `/antigravity/v1/messages` endpoint. |
| **GeminiCLI** | Direct access to native Gemini models via Google's generative AI API. Uses `/geminicli/v1/messages` endpoint. |
| **Vertex AI (vrtx)** | Google Cloud's ML platform. Tool IDs with `toolu_vrtx_` prefix indicate Antigravity/Vertex routing. |
| **thoughtSignature** | Cryptographic signature for thinking blocks. Required by Gemini API to validate thinking content integrity. |
| **Thinking Recovery** | Mechanism to fix corrupted conversation states when thinking blocks are missing or invalid. |

### Signature Availability by Model/API

| Model Type | API | Has `thoughtSignature`? | Tool ID Format |
|------------|-----|------------------------|----------------|
| Claude models (`claude-opus-4-5-thinking`, etc.) | Antigravity | **NO** | `toolu_vrtx_...` |
| Gemini models (`gemini-2.5-flash`, `gemini-2.5-pro`, etc.) | GeminiCLI | **YES** | `toolu_...` (no vrtx) |

**Key insight**: The Antigravity API does NOT return `thoughtSignature` on `functionCall` responses for Claude models. This is an upstream API limitation, not a bug in gcli2api.

### Debugging Signature Issues

Check if signatures are being received from upstream:
```bash
journalctl --user -u gcli2api --no-pager -n 200 | grep -E "SIGNATURE_TRACE|UPSTREAM_RAW"
```

Key log patterns:
- `[SIGNATURE_TRACE] RESPONSE functionCall: has_sig=True` → Signature received (good)
- `[SIGNATURE_TRACE] RESPONSE functionCall: has_sig=False` → No signature from upstream
- `[UPSTREAM_RAW] NO thoughtSignature in functionCall response!` → Upstream not providing signatures
- `[SignatureCache] CACHED signature` → Signature cached for later use
- `[SignatureCache] MISS` → Cache lookup failed, using placeholder

### Debugging Thinking Recovery

Check thinking recovery behavior:
```bash
journalctl --user -u gcli2api --no-pager -n 200 | grep -E "ThinkingRecovery"
```

Key log patterns:
- `[ThinkingRecovery] SKIPPING tool loop recovery` → Recovery skipped (expected when no signatures from upstream)
- `[ThinkingRecovery] Applying recovery for interrupted tool` → User interrupted a tool call
- `[ThinkingRecovery] No signatures found in N tool_use blocks` → Detecting placeholder signature usage

### Debugging Thinking Budget

Check if thinking budget is being set correctly:
```bash
journalctl --user -u gcli2api --no-pager -n 100 | grep -E "thinking.*budget|thinkingBudget"
```

**Important**: Gemini API requires minimum `budget_tokens: 1024`. If client sends lower value, gcli2api auto-corrects:
- `budget_tokens < 1024` → Automatically increased to 1024
- Log shows: `[ANTHROPIC2GEMINI] budget_tokens X below minimum 1024, using 1024`

### Common Issues and Solutions

#### 1. Repetitive Tool Calls
**Symptom**: Model makes the same tool call repeatedly.

**Possible causes**:
- Thinking recovery injecting `[Continue]` messages when not needed
- Missing signatures causing conversation state confusion

**Debug**:
```bash
journalctl --user -u gcli2api --no-pager -n 500 | grep -E "ThinkingRecovery|Continue"
```

**Solution**: The `_check_if_using_placeholder_signatures()` function detects when upstream doesn't provide signatures and skips recovery to prevent this.

#### 2. "budget_tokens: Input should be greater than or equal to 1024" Error
**Symptom**: API rejects request with thinking budget error.

**Cause**: Client sent `budget_tokens` below 1024.

**Solution**: gcli2api now auto-corrects to minimum 1024. Check logs for the correction:
```bash
journalctl --user -u gcli2api --no-pager -n 100 | grep "below minimum 1024"
```

#### 3. Signature Cache Always Empty
**Symptom**: `cache_size=0` in logs despite many tool calls.

**Cause**: Using Claude via Antigravity (doesn't return signatures) or signatures being stripped.

**Debug**:
```bash
journalctl --user -u gcli2api --no-pager -n 100 | grep "SignatureCache"
```

**Expected behavior**: For Antigravity/Claude models, cache will be empty. Placeholder `skip_thought_signature_validator` is used instead.

#### 4. Cross-Model Signature Mismatch
**Symptom**: Errors when switching between Claude and Gemini models mid-conversation.

**Cause**: Signatures are model-family specific. Claude signatures invalid for Gemini and vice versa.

**Solution**: `strip_invalid_thinking_blocks()` removes incompatible signatures. Thinking recovery handles the transition.

## Web Search (Google Search Grounding)

gcli2api provides web search capability via Gemini's native `googleSearch` grounding feature. This allows models to access real-time information from the web.

### Supported Models by Endpoint

**GeminiCLI Endpoint (`/v1/...`)** - Recommended for search

| Model | Search | Notes |
|-------|--------|-------|
| `gemini-2.5-flash` | ✓ | Stable, recommended |
| `gemini-2.5-pro` | ✓ | May have capacity issues |
| `gemini-3-flash-preview` | ✓ | Best quality, full URL citations |
| `gemini-3-pro-preview` | ✓ | Best quality, full URL citations |

**Antigravity Endpoint (`/antigravity/v1/...`)**

| Model | Search | Notes |
|-------|--------|-------|
| `gemini-2.5-flash` | ✓ | Only model with googleSearch support |
| `gemini-3*` + OpenCode UA | stripped | Keep Antigravity path; remove `web_search` to avoid unsupported hangs/503 |
| `gemini-3*` + non-OpenCode UA | rerouted | Forward to GeminiCLI Anthropic route for working search |
| Other Gemini models | auto-stripped | Search tools removed if unsupported |
| Claude models | intercepted | Handled by `web_search_handler.py` (separate Gemini call) |

### How to Enable Search

**GeminiCLI - OpenAI format with `-search` suffix (recommended):**
```bash
curl -s -X POST "http://127.0.0.1:7861/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PASSWORD" \
  -d '{
    "model": "gemini-3-flash-preview-search",
    "messages": [{"role": "user", "content": "What is the current Bitcoin price? Include full source URLs."}]
  }'
```

**GeminiCLI - Anthropic format with web_search tool:**
```bash
curl -s -X POST "http://127.0.0.1:7861/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $PASSWORD" \
  -d '{
    "model": "gemini-2.5-flash",
    "max_tokens": 2048,
    "stream": false,
    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    "messages": [{"role": "user", "content": "Latest news about AI"}]
  }'
```

**Antigravity - Anthropic format only (no `-search` suffix support):**
```bash
curl -s -X POST "http://127.0.0.1:7861/antigravity/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $PASSWORD" \
  -d '{
    "model": "gemini-2.5-flash",
    "max_tokens": 2048,
    "stream": false,
    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    "messages": [{"role": "user", "content": "Bitcoin price? Output full URLs for each source."}]
  }'
```

**Native Gemini format:**
```bash
curl -s -X POST "http://127.0.0.1:7861/v1/models/gemini-2.5-flash:generateContent?key=$PASSWORD" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "Bitcoin price now?"}]}],
    "tools": [{"googleSearch": {}}]
  }'
```

### Endpoint Summary

| Endpoint | Format | Search Method |
|----------|--------|---------------|
| `/v1/chat/completions` | OpenAI | `-search` suffix ✓ |
| `/v1/messages` | Anthropic | `web_search` tool ✓ |
| `/antigravity/v1/messages` | Anthropic | Native on `gemini-2.5-flash`; `gemini-3*` OpenCode strips search, others reroute to GeminiCLI |
| `/antigravity/v1/chat/completions` | OpenAI | ✗ Not supported |

### Getting Citations

To get sources and URLs in responses, explicitly ask for them:
```
"What is the current Bitcoin price? Provide your sources and citations with URLs."
```

The model will include inline citation numbers and a full source list with clickable URLs.

### How Search Works Internally

1. When `web_search_20250305` tool or `-search` suffix is detected
2. gcli2api maps it to Gemini's native `googleSearch` tool
3. The same Gemini model executes the search internally (not a separate model)
4. Results are grounded in real-time Google Search data

**Key files:**
- `src/converter/anthropic2gemini.py`: Maps `web_search` → `googleSearch`
- `src/converter/gemini_fix.py`: Handles `-search` model suffix
- `src/converter/web_search_handler.py`: Agentic search loop for Claude (not yet integrated)

### Parallel Search Limitations

Gemini's googleSearch grounding is rate-limited per account. When Claude Code fires multiple web searches in parallel:
- **2 parallel searches**: Reliable, all return results
- **3+ parallel searches**: Some return `no_results` due to rate limiting
- gcli2api auto-retries empty results (up to 2 retries with backoff), which helps recover from transient throttling
- If building tools that use web search, limit concurrent searches to **2 at a time**

### Search Model Configuration

The default search executor model is defined in `src/converter/web_search_handler.py`:
```python
SEARCH_MODEL = "gemini-2.5-flash"
```

This is used when Claude models need search (future feature).

## Companion Tools

### mcp-gcli2api-search

MCP server for web search via gcli2api's Gemini googleSearch integration.

**Repository**: https://github.com/khvnhtung/mcp-gcli2api-search

**Tools provided**:
- `web_search`: Search the web using Google Search grounding
- `fetch_page`: Fetch and summarize content from a URL

**Quick commands**:
```bash
# Check service status
systemctl --user status mcp-gcli2api-search

# Restart service
systemctl --user restart mcp-gcli2api-search

# View logs
journalctl --user -u mcp-gcli2api-search -f

# Health check
curl -s http://localhost:3100/health | jq

# Test search
curl -s -X POST http://localhost:3100/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"web_search","arguments":{"query":"test"}}}' | jq
```

**Configuration** (in `~/.config/systemd/user/mcp-gcli2api-search.service`):
- `GCLI2API_URL`: gcli2api server URL (default: `http://127.0.0.1:7861`)
- `GCLI2API_PASSWORD`: API password
- `SEARCH_MODEL`: Model for search (`gemini-3-flash` or `gemini-3-pro-high`)
- `THINKING_BUDGET`: Thinking tokens (0=off, 1024-32000)

**Note**: The MCP server forces `gemini-3-flash` if any `2.5` model is specified. This is intentional to deprecate older models.

## Antigravity Porting Decision Log

Auditable record of every design decision made when building the Antigravity proxy. Each entry documents what was decided, why, where in the code, and whether it's a REPLICATION of real client behavior, a WORKAROUND for an API quirk, or a CUSTOM enhancement.

### Headers

| Decision | Type | Location | Notes |
|----------|------|----------|-------|
| Dynamic UA version via updater API (`/api/update/{plat}/stable/0.0.1`), parses `productVersion` from JSON response. Fallback `1.107.0` | REPLICATION | `src/utils.py:19-89` | Old code hit bare root URL and got server version (`1.16.5`) instead of client version (`1.107.0`). Fixed 2026-02-15. |
| Arch uses Electron-style names: `x64`, `arm64` (not `x86_64`) | REPLICATION | `src/utils.py:85-93` | Real client sends `linux/x64`. Old code sent `linux/x86_64`. Fixed 2026-02-15. |
| GeminiCLI UA: `GeminiCLI/0.1.5 (Windows; AMD64)` | REPLICATION | `src/utils.py:17` | Mimics official CLI. Static, not dynamically versioned. |
| `requestType: agent` (or `image_gen` for image models) | REPLICATION | `src/api/antigravity.py:241-249` | Required by upstream routing |
| `requestId: req-<uuid4>` on every request | REPLICATION | `src/api/antigravity.py:238` | Tracing ID |
| **GeminiCLI must NOT send `requestType`/`requestId`** | WORKAROUND | Tested in scripts/ | GeminiCLI tokens get 403 with Antigravity-style headers |

### Payload / Request Format

| Decision | Type | Location | Notes |
|----------|------|----------|-------|
| Strip all `cache_control` fields from content blocks | WORKAROUND | `src/converter/anthropic2gemini.py:111-171` | Claude Code sends them; Cloud Code API rejects "Extra inputs" |
| Inject placeholder property into empty `type: object` schemas | WORKAROUND | `src/converter/anthropic2gemini.py:570-603` | Gemini rejects empty schemas. Fixes Notion MCP. Same approach as Antigravity-Manager |
| Force `maxOutputTokens: 64000` | CUSTOM | `src/converter/gemini_fix.py:418-419` | Prevent truncation from client limits |
| Force `topK: 64` | CUSTOM | `src/converter/gemini_fix.py:420-421` | Clamped to known-working value |
| Flatten `$ref`/`$defs`, merge `anyOf`/`oneOf` in tool schemas | WORKAROUND | `src/converter/anthropic2gemini.py:606+` | Gemini doesn't support JSON Schema references |
| Antigravity payload wrapped as `{model, project, request}` | REPLICATION | `src/api/antigravity.py:356-360` | v1internal envelope format |
| System instruction preamble injection for Antigravity | REPLICATION | `src/converter/gemini_fix.py:267-279` | Agent persona expected by backend |
| Interleaved thinking hint for Claude MCP scenarios | REPLICATION | `src/converter/gemini_fix.py:335-344` | Ported from `antigravity-claude-proxy` |
| Claude uses snake_case `thinkingConfig` fields | REPLICATION | `src/converter/gemini_fix.py:293-309` | Antigravity backend expects different casing per model family |
| All 10 safety categories set to `BLOCK_NONE` | CUSTOM | `src/utils.py:219-229` | Prevent false-positive filtering for developer/agent use |

### Thinking Budget / Effort

| Decision | Type | Location | Notes |
|----------|------|----------|-------|
| Clamp `budget_tokens` minimum to 1024 | WORKAROUND | `src/converter/anthropic2gemini.py:1256-1261` | Gemini API hard minimum |
| Map Claude `adaptive` thinking → fixed 32000 tokens | WORKAROUND | `src/converter/anthropic2gemini.py:1266-1268` | Antigravity has no true adaptive mode |
| Map `effortLevel` (LOW/MEDIUM/HIGH) → `thinkingBudget` (4096/16384/32000) | WORKAROUND | `src/api/antigravity.py:362-384` | Antigravity rejects `effortLevel` with 400; emulate via budget. HIGH=32000 matches Claude Code's default (31999 rounded). |

### Thinking / Signature Handling

| Decision | Type | Location | Notes |
|----------|------|----------|-------|
| Encode `thoughtSignature` into tool IDs via `__thought__` separator | REPLICATION | `src/converter/thoughtSignature_fix.py:50-94` | Only way to survive client round-trips. Ported from `antigravity-claude-proxy` |
| Placeholder `skip_thought_signature_validator` when upstream returns no sig | WORKAROUND | `src/converter/anthropic2gemini.py:1054` | Claude on Antigravity never returns signatures (upstream limitation) |
| Signature cache with 30min TTL + session tracking (2hr TTL) | REPLICATION | `src/converter/thoughtSignature_fix.py:22-28` | Handles message edit/retry. Ported from Rust impl |
| Skip thinking recovery when using placeholder signatures | WORKAROUND | `src/converter/thinking_recovery.py:548-557` | Injecting `[Continue]` with fake sigs causes repetitive tool calls |
| Strip cross-model signatures on model family switch | CUSTOM | `src/converter/thoughtSignature_fix.py:31-47` | Claude sigs invalid for Gemini and vice versa |
| Inject `[Tool call was interrupted.]` on interrupted tool calls | CUSTOM | `src/converter/thinking_recovery.py:543-546` | Close invalid tool loops |

### Error Handling / Retry

| Decision | Type | Location | Notes |
|----------|------|----------|-------|
| Multi-endpoint fallback: Sandbox → Daily → Prod | REPLICATION | `config.py:727-748`, `src/api/antigravity.py:88-151` | Matches Antigravity-Manager's `should_try_next_endpoint` |
| Classify 429 into QUOTA_EXHAUSTED vs MODEL_CAPACITY_EXHAUSTED | CUSTOM | `src/api/retry_strategy.py:160-214` | Rotate account on quota; keep account on capacity |
| Progressive backoff: quota=[60s,5m,30m,2h], capacity=[5s-60s] | CUSTOM | `src/api/retry_strategy.py` | Different error types need different strategies |
| Rotate credential on 401/403 auth errors | CUSTOM | `src/api/retry_strategy.py` | Credential-specific problem |
| Model-level cooldowns (not global) | CUSTOM | `src/credential_manager.py` | Quota is per-model per-account |

### Web Search Routing

| Decision | Type | Location | Notes |
|----------|------|----------|-------|
| Map `web_search_20250305` → Gemini `googleSearch` | CUSTOM | `src/converter/anthropic2gemini.py:826-862` | Translate Anthropic tool format |
| `-search` model suffix adds `googleSearch` automatically | CUSTOM | `src/converter/gemini_fix.py:248-261` | Convenience for OpenAI-format clients |
| Antigravity: only `gemini-2.5-flash` supports native search | WORKAROUND | `src/router/antigravity/anthropic.py:348-357` | Other models hang or 503 with googleSearch |
| Gemini-3 + OpenCode UA: strip `web_search` silently | WORKAROUND | `src/router/antigravity/anthropic.py:317-325` | Keep OpenCode on Antigravity path |
| Gemini-3 + non-OpenCode UA: reroute to GeminiCLI | WORKAROUND | `src/router/antigravity/anthropic.py:308-346` | Search works on GeminiCLI endpoint |
| Claude + `web_search`: intercept, use Gemini as search executor | WORKAROUND | `src/router/antigravity/anthropic.py:286-295` | Claude on Antigravity can't do googleSearch |

### Credential / Auth

| Decision | Type | Location | Notes |
|----------|------|----------|-------|
| Same OAuth Client IDs as official clients | REPLICATION | `src/utils.py:92-93` | Required for valid tokens |
| Default endpoint: `daily-cloudcode-pa.sandbox.googleapis.com` | REPLICATION | `config.py:716,730` | Least restrictive endpoint |

### Transport (HTTP/2)

| Decision | Type | Location | Notes |
|----------|------|----------|-------|
| **HTTP/2 enabled** for all outbound requests | REPLICATION | `src/httpx_client.py:54` | Matches real Antigravity (ConnectRPC/HTTP/2) and GeminiCLI behavior. `h2` library was already installed via Hypercorn. |

**HTTP/2 Analysis:**

Real clients use HTTP/2. Antigravity uses ConnectRPC (gRPC-web over HTTP/2) per `docs/antigravity-protocol.md`. GeminiCLI also negotiates HTTP/2 via ALPN.

Why we enabled it:
- Real clients always use HTTP/2. HTTP/1.1 + Antigravity UA was a fingerprinting mismatch (ALPN visible server-side).
- Zero migration risk. `h2` library already installed (transitive dep of Hypercorn). One-line change. httpx streaming works identically over HTTP/2.
- Under concurrent load, HTTP/2 multiplexes many requests over fewer TCP connections — better than HTTP/1.1 connection-per-request.

### Investigated But Abandoned

| Investigation | Outcome | Reason |
|---------------|---------|--------|
| **Dual Client ID strategy** (GeminiCLI + Antigravity OAuth for 2x quota) | ABANDONED | Cannot test without exhausted account. `fetchAvailableModels` quota API always reports 100% — too coarse to detect per-request changes. |
| **Proactive quota routing** (call `fetchAvailableModels` before routing) | NOT USEFUL | Quota API reports 1.0 even when partially consumed. Already track exhaustion reactively via 429s. |

### Image Token Estimation

| Decision | Type | Location | Notes |
|----------|------|----------|-------|
| Dimension-based image token estimation (crop-unit tiling formula) | REPLICATION | `src/token_estimator.py:70-143` | Implements Google's documented formula: `crop_unit = clamp(floor(min(w,h)/1.5), 256, 768)`, tiles = `ceil(w/cu) * ceil(h/cu)`, tokens = `tiles * 258`. Replaces flat 300 tokens/image. |
| Extract image dimensions from base64 headers (PNG, JPEG, GIF, WebP) | CUSTOM | `src/token_estimator.py:82-139` | Decodes only first ~4KB of base64 to read binary headers. Zero external dependencies (no PIL/Pillow). |
| Hybrid native+image token counting | CUSTOM | `src/token_estimator.py:count_tokens_native()` | For image payloads: strip images → native countTokens for text → add dimension-based image estimate. Previously skipped native counting entirely when images present. |
| Size-based fallback for unknown image formats | CUSTOM | `src/token_estimator.py:_estimate_tokens_from_data_size()` | When dimensions can't be parsed: <50KB→258, 50-500KB→1548, 500KB-2MB→2064, >2MB→3870 tokens. Conservative overcount preferred. |

**Impact (3x 1080p video frames):**
- Old estimate: 300 tokens/image × 3 = 900 tokens
- New estimate: 1548 tokens/image × 3 = 4644 tokens (5.2x more accurate)
- This means Claude Code's context compaction triggers much earlier, preventing sudden "Context limit reached" errors.

**Image support confirmed:**
- Claude Opus 4.6 on Antigravity: `supports_images = true` (tested 2026-02-15, responded correctly to image input)
- Claude Opus 4.5: deprecated, redirects to 4.6

