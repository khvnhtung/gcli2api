# Antigravity Quota & Credits

> How Antigravity tracks usage, credits, and rate limits.
> Extracted from Antigravity v1.107.0 on 2026-02-15.

## Credits System

### Credits Message

```protobuf
message Credits {
  CreditType credit_type = 1;
  int64 credit_amount = 2;

  enum CreditType {
    CREDIT_TYPE_UNSPECIFIED = 0;
    GOOGLE_ONE_AI = 1;           // Google One AI Premium credits
  }
}
```

Credits are the billing unit. Each API call consumes credits based on the model's `credit_multiplier`.

### How Credits Are Tracked

Credits live in **`UserTier.available_credits`** (repeated `Credits`). The client learns about credits via `LoadCodeAssistResponse`:

```protobuf
message LoadCodeAssistResponse {
  UserTier current_tier = 1;           // User's active tier (has available_credits)
  repeated UserTier allowed_tiers = 2; // All tiers user can access
  optional string cloudaicompanion_project = 3;
  repeated IneligibleTier ineligible_tiers = 5;
  optional bool gcp_managed = 6;
  optional string manage_subscription_uri = 7;
  optional ReleaseChannel release_channel = 8;
  optional string upgrade_subscription_uri = 9;
  optional GeminiCodeAssistSetting gemini_code_assist_setting = 10;
  optional string g1_tier = 11;        // Google One tier identifier
  UserTier paid_tier = 12;             // The paid tier info
}
```

### Per-Model Cost

Each model has a `credit_multiplier` in `ClientModelConfig`:

```protobuf
message ClientModelConfig {
  float credit_multiplier = 3;     // e.g., 1.0 for Flash, 5.0 for Opus
  ModelPricingType pricing_type = 13;  // STATIC_CREDIT, API, or BYOK
  QuotaInfo quota_info = 15;       // Real-time quota state
}
```

**Pricing types**:
- `STATIC_CREDIT` — Fixed credits per request (most free-tier models)
- `API` — Usage-based (token count × rate)
- `BYOK` — Bring Your Own Key (user pays provider directly)

## Quota Info

### QuotaInfo Message

```protobuf
message QuotaInfo {
  float remaining_fraction = 1;           // 0.0 = exhausted, 1.0 = full
  google.protobuf.Timestamp reset_time = 2;  // When quota resets
}
```

This is attached to each `ClientModelConfig` and updated via `GetCascadeModelConfigs`.

### When Quota Is Checked

1. **On app startup**: `LoadCodeAssist` RPC — returns tier info with initial credit balances
2. **Before model selection**: `GetCascadeModelConfigs` — returns per-model `QuotaInfo`
3. **On model status check**: `GetModelStatuses` — returns availability/degradation info
4. **After each request**: Server-side tracking (client doesn't explicitly poll)

### Quota Reset Behavior

- `reset_time` is a UTC timestamp indicating when the quota window resets
- The client UI shows remaining quota as a percentage bar
- When `remaining_fraction` hits 0, the model is shown as unavailable

## Rate Limiting

### Server-Side (Upstream Behavior)

From gcli2api's observations:

| Error Type | HTTP Status | Description | Cooldown |
|------------|-------------|-------------|----------|
| `QUOTA_EXHAUSTED` | 429 | Per-account daily/hourly quota | 4h-6h (progressive) |
| `MODEL_CAPACITY_EXHAUSTED` | 429 | Google infrastructure overload | 5s-60s |
| `RATE_LIMIT_EXCEEDED` | 429 | Per-minute rate limit | 30s |
| `SERVER_ERROR` | 5xx | Server errors | Exponential backoff |

### Client-Side Handling

The Antigravity client does **not** implement sophisticated retry logic. When it receives quota errors:
1. The model's `QuotaInfo.remaining_fraction` is updated to 0
2. The UI disables the model
3. User sees "Quota exhausted" message with `reset_time`
4. User can switch to a different model or wait

This is simpler than gcli2api's approach, which retries with account rotation.

## Ineligible Tiers

When a user can't access certain tiers:

```protobuf
message IneligibleTier {
  IneligibleTierReasonCodes reason_code = 1;
  string reason_message = 2;
  string tier_id = 3;
  string tier_name = 4;
  string validation_error_message = 5;
  string validation_url_link_text = 6;
  string validation_url = 7;

  enum IneligibleTierReasonCodes {
    UNKNOWN = 0;
    INELIGIBLE_ACCOUNT = 1;
    UNKNOWN_LOCATION = 2;
    UNSUPPORTED_LOCATION = 3;
    RESTRICTED_NETWORK = 4;
    RESTRICTED_AGE = 5;
    NON_USER_ACCOUNT = 6;
    DASHER_USER = 7;            // Google Workspace managed account
    BYOID_USER = 8;             // Bring Your Own Identity
    RESTRICTED_DASHER_USER = 9;
    VALIDATION_REQUIRED = 10;
  }
}
```

**Key insight for gcli2api**: `DASHER_USER` (code 7) means Google Workspace accounts may have different access than personal Gmail accounts. This could explain why some accounts hit different rate limits.

## LoadCodeAssist - The Quota Bootstrap

This is the **first RPC called on startup** to determine what the user can access.

### Request

```protobuf
message LoadCodeAssistRequest {
  optional string cloudaicompanion_project = 1;
  ClientMetadata metadata = 2;       // IDE type, version, platform
  optional Mode mode = 3;

  enum Mode {
    MODE_UNSPECIFIED = 0;
    FULL_ELIGIBILITY_CHECK = 1;  // Full check on startup
    HEALTH_CHECK = 2;            // Quick check (periodic)
  }
}
```

### Response

Returns `LoadCodeAssistResponse` (see above) with:
- Current tier + credits
- Allowed/ineligible tiers
- Subscription management URIs
- Release channel info

### gcli2api equivalent

gcli2api already calls `v1internal:fetchAvailableModels` which returns similar data. The `LoadCodeAssist` RPC is the ConnectRPC equivalent.

## Admin Controls

Enterprise accounts can restrict features:

```protobuf
message FetchAdminControlsResponse {
  bool disable_telemetry = 1;
  bool disable_feedback = 2;
  RecitationPolicy recitation_policy = 3;
  GroundingType grounding_type = 4;      // Controls web search
  bool secure_mode_enabled = 5;
  McpSetting mcp_setting = 6;            // MCP server restrictions
  TurboModeSetting turbo_mode_setting = 7;
  BrowserSetting browser_setting = 8;     // Browser tool restrictions
  PreviewFeatureSetting preview_feature_setting = 9;
  AgentSetting agent_setting = 10;        // Allowed models for agent
  CliFeatureSetting cli_feature_setting = 11;
}
```

**Relevant to gcli2api**:
- `GroundingType` shows web search can be `GROUNDING_WITH_GOOGLE_SEARCH` or `WEB_GROUNDING_FOR_ENTERPRISE` — different search modes exist
- `AgentSetting.agy_allowed_models` — admin can restrict which models the agent uses
- `McpSetting.mcp_enabled` + `override_mcp_config_json` — MCP can be admin-controlled

## gcli2api Comparison

| Aspect | Antigravity (Official) | gcli2api |
|--------|----------------------|----------|
| **Quota tracking** | Server-managed via `QuotaInfo` | Client-side tracking in `quota_windows` table |
| **Credits** | `Credits` message with `credit_amount` | Not tracked (inferred from 429 errors) |
| **Cost awareness** | `credit_multiplier` per model | Not tracked |
| **Reset time** | `QuotaInfo.reset_time` from server | Estimated from cooldown patterns |
| **Retry on quota** | No retry, show error to user | Auto-retry with account rotation |
| **Multi-account** | Single account per session | Multiple accounts with rotation |

## Actionable Items for gcli2api

1. **Call `LoadCodeAssist`** proactively to get `QuotaInfo.remaining_fraction` before requests — could avoid wasting retries on exhausted accounts
2. **Track `credit_multiplier`** per model to estimate when quota will run out (Opus costs 5x Flash)
3. **Use `GetCascadeModelConfigs`** periodically to get real-time `QuotaInfo` per model
4. **Detect `DASHER_USER`** accounts that may have enterprise restrictions

## See Also

- [antigravity-models.md](antigravity-models.md) — Model pricing types and tiers
- [antigravity-protocol.md](antigravity-protocol.md) — How RPCs are called
- [antigravity-gcli2api-gaps.md](antigravity-gcli2api-gaps.md) — Full gap analysis
