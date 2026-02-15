# Antigravity → gcli2api Gap Analysis

> What we learned from reverse engineering that can improve gcli2api.
> Updated: 2026-02-15.

## Already Implemented (No Action Needed)

These findings from the reverse engineering are already in gcli2api:

| Finding | gcli2api Implementation |
|---------|------------------------|
| User-Agent format `antigravity/<ver> <os>/<arch>` | `src/utils.py:69-85` |
| OAuth token refresh flow | `src/google_oauth_api.py` |
| Quota refresh via `v1internal:fetchAvailableModels` | `src/api/quota_refresh.py` |
| 429 error classification (QUOTA_EXHAUSTED vs CAPACITY) | `src/api/retry_strategy.py` |
| Account rotation on quota exhaustion | `src/api/retry_strategy.py` |
| Dynamic model list from upstream | `src/api/antigravity.py` |

## High-Priority Improvements

### 1. Proactive Quota Checking via `QuotaInfo`

**What we learned**: Antigravity gets `QuotaInfo.remaining_fraction` (0.0-1.0) and `reset_time` per model via `GetCascadeModelConfigs`.

**Current gcli2api behavior**: We only learn about quota exhaustion *after* hitting a 429. This wastes a request and causes latency.

**Improvement**: Before routing a request to an account, check if `remaining_fraction` is near 0 and skip that account. This requires:
- Calling `GetCascadeModelConfigs` periodically (or after each 429)
- Storing `remaining_fraction` + `reset_time` per account per model
- Using this data in the account selection logic

**Files to modify**: `src/api/quota_refresh.py`, `src/credential_manager.py`

**Priority**: HIGH — reduces wasted requests and latency

### 2. Credit Multiplier Awareness

**What we learned**: Each model has a `credit_multiplier`. Opus costs significantly more than Flash per request.

**Current gcli2api behavior**: All models treated equally for quota tracking. An account that made 10 Opus requests is treated the same as one that made 10 Flash requests.

**Improvement**: Weight quota consumption by `credit_multiplier`:
- Track `estimated_credits_used` per account (sum of `credit_multiplier` per request)
- When rotating accounts, prefer the one with most remaining credits
- Log credit consumption in audit

**Files to modify**: `src/audit_log.py`, `src/api/retry_strategy.py`

**Priority**: MEDIUM — better account utilization

### 3. Dual OAuth Client ID Strategy

**What we learned**: 
- Antigravity uses client ID: `1071006060591-tmhssin2h21lcre235vtolojh4g403ep`
- GeminiCLI uses client ID: `681255809395-...`
- These may have **different rate limit buckets**

**Current gcli2api behavior**: All accounts use the GeminiCLI client ID.

**Improvement**: Test whether accounts using the Antigravity client ID get separate quota. If so:
- Register some accounts with the Antigravity client ID
- Route Antigravity-endpoint requests through Antigravity-registered accounts
- Effectively double per-account quota

**Files to modify**: `src/google_oauth_api.py`, `config.py`

**Priority**: HIGH — could double effective quota if confirmed

### 4. LoadCodeAssist for Account Health

**What we learned**: `LoadCodeAssist` returns `current_tier`, `available_credits`, `ineligible_tiers`, and `g1_tier`.

**Current gcli2api behavior**: No pre-flight check on account health. We discover issues (banned, wrong tier, etc.) only when requests fail.

**Improvement**: Call `LoadCodeAssist` on startup and periodically for each account:
- Detect banned/restricted accounts before routing traffic to them
- Know which accounts have `GOOGLE_ONE_AI` credits
- Detect `DASHER_USER` accounts with enterprise restrictions
- Cache `available_credits` to predict when quota runs out

**Files to modify**: `src/credential_manager.py`, new `src/api/account_health.py`

**Priority**: MEDIUM — better account management

### 5. Codename Model Probing

**What we learned**: Google has many unreleased models with codenames (RIFTRUNNER, COSMICFORGE, etc.).

**Current gcli2api behavior**: Only supports known model names.

**Improvement**: Periodically probe codename models via the Antigravity API to:
- Discover when new models become available
- Map codenames to actual capabilities
- Add them to the available models list

**Files to modify**: New script or cron job

**Priority**: LOW — nice to have, not critical

## Medium-Priority Improvements

### 6. Better IDE Type Spoofing

**What we learned**: The `ClientMetadata.IdeType` enum includes:
- `ANTIGRAVITY = 9` (the app itself)
- `JETSKI = 10` (the agent)
- `GEMINI_CLI = 14` (GeminiCLI)

**Current gcli2api behavior**: Uses GeminiCLI user-agent for GeminiCLI routes, Antigravity user-agent for Antigravity routes.

**Improvement**: If the upstream API treats `JETSKI` differently (e.g., higher limits for agents), we could use that IDE type for agent-style requests.

**Files to modify**: `src/utils.py`

**Priority**: LOW — speculative

### 7. Admin Controls Detection

**What we learned**: `FetchAdminControlsResponse` can reveal:
- Whether MCP is enabled/disabled
- Browser tool restrictions  
- Allowed models for agents
- Grounding type (search mode)

**Current gcli2api behavior**: No awareness of admin controls.

**Improvement**: Call `FetchAdminControls` per account to detect restrictions. Skip restricted accounts for features they can't use (e.g., don't route search requests to accounts with search disabled).

**Files to modify**: `src/credential_manager.py`

**Priority**: LOW — only matters for enterprise/Workspace accounts

## Key Extracted Files Reference

| gcli2api file | What to look at in Antigravity | Improvement area |
|---------------|-------------------------------|------------------|
| `src/api/quota_refresh.py` | `QuotaInfo`, `LoadCodeAssist` | Proactive quota checking |
| `src/api/retry_strategy.py` | `credit_multiplier`, error classification | Better rotation |
| `src/google_oauth_api.py` | Antigravity's OAuth client ID | Dual client ID |
| `src/credential_manager.py` | `LoadCodeAssist`, `IneligibleTier` | Account health |
| `src/utils.py` | `ClientMetadata.IdeType`, User-Agent | IDE spoofing |
| `src/audit_log.py` | `Credits`, `credit_amount` | Credit tracking |

## Extracted Proto Schema

Full proto schema with all message definitions: `/tmp/antigravity_proto_schema.proto`
(Also documented inline in [antigravity-models.md](antigravity-models.md) and [antigravity-quota.md](antigravity-quota.md))

## See Also

- [antigravity-reverse-engineering.md](antigravity-reverse-engineering.md) — How to extract data
- [antigravity-protocol.md](antigravity-protocol.md) — Endpoints and transport
- [antigravity-models.md](antigravity-models.md) — Model details
- [antigravity-quota.md](antigravity-quota.md) — Quota system
- [antigravity-cascade-flow.md](antigravity-cascade-flow.md) — Chat flow
