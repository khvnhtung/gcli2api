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
