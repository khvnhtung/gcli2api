# Enhanced web_search Tool Design

**Date**: 2026-02-02
**Goal**: Add Perplexity-like filtering and citation features to `web_search` tool

## Overview

Enhance the existing `web_search` tool to support:
- Domain filtering (allowed/blocked)
- Recency filtering (day/week/month/year)
- Inline citations with source URLs

## Tool Schema

```json
{
  "name": "web_search",
  "description": "Search the web for current information with optional filtering.",
  "input_schema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "The search query"
      },
      "recency_filter": {
        "type": "string",
        "enum": ["day", "week", "month", "year"],
        "description": "Filter results by recency"
      },
      "max_results": {
        "type": "integer",
        "description": "Maximum number of results (1-10, default 5)"
      }
    },
    "required": ["query"]
  }
}
```

## Implementation

### 1. Recency Hints (web_search_handler.py)

```python
RECENCY_HINTS = {
    "day": "from the last 24 hours",
    "week": "from the past week",
    "month": "from the past month",
    "year": "from the past year",
}

# Added to prompt when recency_filter is set
prompt = f"Search the web for: {modified_query}"
if recency_filter:
    prompt += f"\n\nFocus on results {RECENCY_HINTS[recency_filter]}."
```

### 2. Grounding Metadata Extraction

```python
def extract_grounding_sources(response_data: dict) -> list[dict]:
    """Extract sources from Gemini's groundingMetadata."""
    sources = []
    metadata = response_data.get("groundingMetadata", {})
    chunks = metadata.get("groundingChunks", [])

    for chunk in chunks:
        web = chunk.get("web", {})
        if web:
            sources.append({
                "title": web.get("title", ""),
                "url": web.get("uri", ""),
            })

    return sources
```

### 3. Inline Citation Formatting

```python
def format_response_with_citations(text: str, sources: list[dict]) -> str:
    """Append inline citations to response text."""
    if not sources:
        return text

    # Deduplicate by URL
    seen = set()
    unique_sources = []
    for s in sources:
        if s["url"] and s["url"] not in seen:
            seen.add(s["url"])
            unique_sources.append(s)

    if not unique_sources:
        return text

    lines = [text, "", "---", "**Sources:**"]
    for i, src in enumerate(unique_sources[:10], 1):
        title = src["title"] or src["url"]
        lines.append(f"[{i}] {title}: {src['url']}")

    return "\n".join(lines)
```

## Files to Modify

| File | Changes |
|------|---------|
| `gcli2api/src/converter/web_search_handler.py` | Add query building, grounding extraction, citation formatting |
| `mcp-gcli2api-search/index.js` | Update tool schema, forward new params |

## Output Example

```
The latest research on transformer architectures shows significant advances in efficiency...

---
**Sources:**
[1] Attention Is All You Need: https://arxiv.org/abs/1706.03762
[2] BERT: Pre-training of Deep Bidirectional Transformers: https://arxiv.org/abs/1810.04805
```

## Constraints

- Recency filtering uses prompt hints (best-effort, not guaranteed)
- `max_results` is a hint to the model (Gemini doesn't expose result count control)
- Citations extracted from `groundingMetadata.groundingChunks[].web`
- Domain filtering was removed as Gemini's googleSearch doesn't enforce site: operators
