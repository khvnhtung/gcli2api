# Antigravity Thinking Budgets — Server-Provided Ground Truth

> Per-model thinking configuration from `v1internal:fetchAvailableModels`.
> Probed: 2026-02-18 from `daily-cloudcode-pa.sandbox.googleapis.com`.

## Architecture

The official Antigravity app (v1.107.0) is a **thin client** with ZERO client-side
thinking budget logic. The terms `thinkingConfig`, `thinkingLevel`, `effortLevel`,
`includeThoughts`, `budgetTokens`, and `adaptive` do not exist anywhere in the
app's JavaScript (confirmed by exhaustive search of `main.js` and
`jetskiAgent/main.js`).

Thinking is controlled by:
1. **Model selection** — distinct enum values for thinking variants
   (e.g., `CLAUDE_4_OPUS_THINKING` = 291 vs `CLAUDE_4_OPUS` = 290)
2. **Server-side `ModelDetails`** — the server advertises `thinkingBudget`,
   `minThinkingBudget`, and `supportsThinking` per model via `fetchAvailableModels`

The client never sends thinking configuration in requests. The Language Server
(compiled Go binary) handles all thinking budget, signature, and configuration
logic before calling the upstream model API.

## Server-Provided Thinking Budgets (Live Data)

| Model | supportsThinking | thinkingBudget | minThinkingBudget | supportsRawThinking | maxOutputTokens | maxTokens |
|---|---|---|---|---|---|---|
| `claude-opus-4-6-thinking` | true | **1024** | 0 | false | 64000 | 200000 |
| `claude-sonnet-4-5` | false | 0 | 0 | false | 64000 | 200000 |
| `claude-sonnet-4-5-thinking` | true | **1024** | 0 | false | 64000 | 200000 |
| `claude-sonnet-4-6` | *(absent)* | 0 | 0 | *(absent)* | 64000 | 200000 |
| `gemini-2.5-flash` | true | **1024** | 0 | false | 65535 | 1048576 |
| `gemini-2.5-flash-thinking` | true | **1024** | 0 | false | 65535 | 1048576 |
| `gemini-2.5-flash-lite` | false | 0 | 0 | false | 65535 | 1048576 |
| `gemini-2.5-pro` | true | **1024** | 128 | false | 65535 | 1048576 |
| `gemini-3-flash` | true | **-1** | 32 | false | 65536 | 1048576 |
| `gemini-3-pro-high` | true | **-1** | 128 | false | 65535 | 1048576 |
| `gemini-3-pro-low` | true | **128** | 128 | false | 65535 | 1048576 |
| `gemini-3-pro-image` | false | 0 | 0 | false | 0 | 0 |
| `gpt-oss-120b-medium` | true | **8192** | 0 | false | 32768 | 114000 |

### Key Observations

1. **`thinkingBudget: -1` = native dynamic thinking** (Gemini 3 Flash and Pro High).
   The model decides its own thinking depth per request. This is NOT `thinkingLevel`
   (a concept from antigravity-manager, not the official API).

2. **Default for Claude and Gemini 2.5 is 1024** — much lower than what coding
   clients like OpenCode/Claude Code typically request (32000). This is the server's
   *default*, not a maximum. Clients override it.

3. **`gemini-3-pro-low` has budget = minBudget = 128** — fixed minimal thinking.
   `gemini-3-pro-high` has -1 (dynamic). The thinking level is baked into the
   model variant, not a per-request parameter.

4. **`minThinkingBudget` varies by model**: 0 (Claude, Gemini 2.5 Flash), 32
   (Gemini 3 Flash), 128 (Gemini 2.5 Pro, Gemini 3 Pro).

5. **`supportsRawThinking` is false for ALL models** — not yet active.

6. **`gpt-oss-120b-medium` has thinking** with budget 8192. This is OpenAI's
   120B model via Vertex AI.

## Additional Server Metadata

```
defaultAgentModelId: gemini-3-pro-high
commandModelIds: [gemini-3-flash, gemini-3-pro-low]
webSearchModelIds: [gemini-2.5-flash]
imageGenerationModelIds: [gemini-3-pro-image]
commitMessageModelIds: [gemini-2.5-flash]
deprecatedModelIds:
  claude-opus-4-5-thinking → claude-opus-4-6-thinking
```

## Implications for gcli2api

### Current gcli2api behavior vs server truth

| Setting | gcli2api current | Server truth | Impact |
|---|---|---|---|
| Minimum thinking budget | Hardcoded 1024 for all models | 0 (Claude), 32 (G3 Flash), 128 (G2.5 Pro, G3 Pro) | gcli2api rejects valid low budgets |
| Gemini 3 adaptive | Sends fixed integer thinkingBudget | -1 (native dynamic) | May not trigger optimal thinking |
| Claude default budget (no config) | No auto-injection | Server default 1024 | Consistent—neither injects |
| maxOutputTokens | Forced 64000 for all | 64000 (Claude), 65535-65536 (Gemini) | Close enough |

### Recommended changes

1. **Use -1 for Gemini 3 adaptive mode** instead of mapping to fixed budgets.
   When OpenCode sends `thinking: { type: "adaptive" }` and the target is
   Gemini 3, set `thinkingBudget: -1` to let the model decide.

2. **Lower minThinkingBudget floor** from hardcoded 1024 to per-model minimums
   (32 for Gemini 3 Flash, 128 for Pro, 0 for Claude).

3. **Consider caching `fetchAvailableModels` thinking data** to dynamically
   apply correct budgets and minimums as Google updates them.

## Source Attribution

> **IMPORTANT**: All data in this document comes directly from:
> - The official Antigravity app binary at `/usr/share/antigravity/resources/app/`
> - Live API probe of `v1internal:fetchAvailableModels`
>
> No data from third-party re-implementations (e.g., antigravity-manager,
> antigravity-claude-proxy) is included. See `antigravity-attribution-notes.md`
> for details on what comes from which source across all docs.

## See Also

- [antigravity-models.md](antigravity-models.md) — Model enums and codenames
- [antigravity-quota.md](antigravity-quota.md) — Credits and quota system
- [antigravity-attribution-notes.md](antigravity-attribution-notes.md) — Source attribution
