# Antigravity Source Attribution Notes

> Clarification of which data in our docs comes from the official Antigravity
> app versus third-party re-implementations.
> Written: 2026-02-18.

## The Distinction

There are three different "Antigravity" codebases in play:

| Source | Type | Language | Description |
|---|---|---|---|
| **Official Antigravity app** | Google's VS Code fork | JS (Electron) + Go (Language Server) | The real app at `/usr/share/antigravity/` |
| **antigravity-manager** | Third-party proxy | Rust | Re-implementation that also reverse-engineered the protocol |
| **gcli2api** | Third-party proxy | Python | This project |

## What Is Confirmed from the Official App

These findings come directly from the official binary (v1.107.0) and live API:

- **Protocol**: ConnectRPC v2 over HTTP/2 with binary Protobuf serialization
- **Endpoints**: `daily-cloudcode-pa.googleapis.com` (prod), sandbox variant
- **OAuth Client ID**: `1071006060591-tmhssin2h21lcre235vtolojh4g403ep`
- **Model enums**: All 60+ model IDs extracted from `main.js`
- **Proto schema**: `ModelInfo`, `ModelDetails`, `ClientModelConfig`, `QuotaInfo`, etc.
- **`skip_thought_signature_validator`**: Real sentinel value in the Language Server Go binary
- **Dual signatures**: `thought_signature` (string) + `thinking_signature` (bytes)
- **Thinking budgets**: Server-provided via `fetchAvailableModels` (see `antigravity-thinking-budgets.md`)
- **Architecture**: Client is a thin UI; ALL thinking config is server-side
- **`CLIENT_LOOPING`** (stop reason 19): Official detection of repetitive tool calls
- **No client-side thinking logic**: Zero occurrences of `thinkingConfig`, `thinkingLevel`, `effortLevel`, `includeThoughts`, `adaptive`, `budgetTokens` in any client JS

## What Came from antigravity-manager (NOT the Official App)

These values/behaviors were incorrectly attributed to "the official app" in earlier
analysis. They are antigravity-manager design choices:

| Claim | Source | Reality |
|---|---|---|
| Auto-enable thinking with budget=10000 for Opus/Pro | antigravity-manager | Official app: no auto-enable, server default is 1024 |
| Adaptive Claude budget = 16000 | antigravity-manager | Official app: server default is 1024, clients override |
| Budget cap at 24576 for Gemini | antigravity-manager | Official app: no client-side cap, server uses -1 for dynamic |
| `thinkingLevel` for Gemini 3 (string "low"/"medium"/"high") | antigravity-manager | Official app: zero occurrences of `thinkingLevel` |
| Smart-downgrade (3 conditions) | antigravity-manager | Official app: no client-side thinking logic at all |
| Content reordering (thinking → text → toolUse) | antigravity-manager | Official app: server-side via `group_tools_with_planner_response` |
| Cross-model sub-family signature validation | antigravity-manager | Official app: handled by Language Server, not client |
| `redacted_thinking` → `[Redacted Thinking: data]` text | antigravity-manager | Official app: uses `thinkingRedacted` boolean flag |
| maxOutputTokens > thinkingBudget + 8192 check | antigravity-manager | No evidence in official app |

## What gcli2api Implements as Custom Enhancements

These features exist in gcli2api but NOT in the official app (because the official
app's architecture makes them unnecessary):

| Feature | Why gcli2api needs it |
|---|---|
| Signature caching (3-layer) | JSON APIs strip unknown fields; Protobuf preserves them |
| Tool ID `__thought__` encoding | Same reason — signature must survive JSON round-trip |
| Thinking recovery (interrupted tools, loops) | Official app's Language Server prevents these states |
| Cross-model signature stripping | Official app doesn't support mid-conversation model switching |
| Content reordering | Anthropic API uses ordered content blocks; Protobuf uses flat fields |
| Effort → budget mapping | Anthropic/OpenAI clients send effort; Gemini API needs budget |

These are justified by the architectural difference: gcli2api proxies between
JSON-based APIs (lossy) while the official app uses Protobuf (lossless) with
server-side state management.

## AGENTS.md Decision Log Corrections

The decision log in `AGENTS.md` should be updated to reflect these attributions.
Items currently marked as `REPLICATION` that are actually from antigravity-manager
(not the official app) should be re-labeled as `CUSTOM` or `THIRD-PARTY-INSPIRED`.

## See Also

- [antigravity-thinking-budgets.md](antigravity-thinking-budgets.md) — Server-provided ground truth
- [antigravity-reverse-engineering.md](antigravity-reverse-engineering.md) — Extraction methodology
- [antigravity-protocol.md](antigravity-protocol.md) — Confirmed official protocol details
