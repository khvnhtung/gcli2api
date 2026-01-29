# AGENTS.md - Coding Agent Guidelines for gcli2api

This document provides guidelines for AI coding agents working on the gcli2api codebase.

## Project Overview

gcli2api converts GeminiCLI and Antigravity to OpenAI, Gemini, and Claude API compatible interfaces.
It's a Python 3.12+ FastAPI application with async/await patterns throughout.

## Critical: Credentials Mount (Docker)

gcli2api stores its SQLite state at `./creds/credentials.db` inside the container.

- Canonical host path (this repo): `./creds/credentials.db`
- Container path: `/app/creds/credentials.db`

If you mount an empty directory (or a different directory that has no `credentials.db`), the app will create a fresh empty database and you will see errors like `没有可用凭证` / "no available credentials".

Recommended container run mount:

```bash
docker run -d --name gcli2api -p 7861:7861 -v ./creds:/app/creds gcli2api:latest
```

Quick sanity checks:

```bash
ls -la ./creds/credentials.db
docker exec gcli2api ls -la /app/creds
```

## Build & Run Commands

```bash
# Install dependencies
make install           # Production dependencies
make install-dev       # Development dependencies (includes pytest, black, flake8, mypy)

# Run the application
make run               # Starts server on port 7861 (configurable via PORT env)
python web.py          # Direct execution

# Docker (preferred for deployment)
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
Handles intelligent retry with exponential backoff:
- 503/529: ExponentialBackoff 10s → 60s (capacity exhausted)
- 429: Parse RetryInfo or LinearBackoff 5s
- 500: LinearBackoff 3s
- 400: NoRetry (client error)

### JSON Schema Cleaning (`src/converter/anthropic2gemini.py`)
Cleans MCP tool schemas for Gemini API compatibility:
- `$ref`/`$defs` flattening
- `anyOf`/`oneOf` merging
- Empty object injection (fixes Notion MCP)

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

4. **Account Rotation**: On 429/401/403/500, rotate to next credential. On 503/529, retry same account with backoff.

5. **Docker Deployment**: Always rebuild image after code changes:
   ```bash
   docker build -t gcli2api:latest . && docker stop gcli2api && docker rm gcli2api && \
   docker run -d --name gcli2api -p 7861:7861 -v ./creds:/app/creds gcli2api:latest
   ```
