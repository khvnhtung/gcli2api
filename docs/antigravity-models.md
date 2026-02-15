# Antigravity Models

> Model enums, codenames, aliases, providers, and tier access.
> Extracted from Antigravity v1.107.0 on 2026-02-15.

## Model Enum (exa.codeium_common_pb.Model)

### Production Models

| Enum ID | Name | Category |
|---------|------|----------|
| 246 | `MODEL_GOOGLE_GEMINI_2_5_PRO` | Gemini |
| 312 | `MODEL_GOOGLE_GEMINI_2_5_FLASH` | Gemini |
| 313 | `MODEL_GOOGLE_GEMINI_2_5_FLASH_THINKING` | Gemini |
| 329 | `MODEL_GOOGLE_GEMINI_2_5_FLASH_THINKING_TOOLS` | Gemini |
| 330 | `MODEL_GOOGLE_GEMINI_2_5_FLASH_LITE` | Gemini |

### Claude Models (via Antigravity/Vertex)

| Enum ID | Name | Category |
|---------|------|----------|
| 281 | `MODEL_CLAUDE_4_SONNET` | Claude |
| 282 | `MODEL_CLAUDE_4_SONNET_THINKING` | Claude |
| 290 | `MODEL_CLAUDE_4_OPUS` | Claude |
| 291 | `MODEL_CLAUDE_4_OPUS_THINKING` | Claude |
| 333 | `MODEL_CLAUDE_4_5_SONNET` | Claude |
| 334 | `MODEL_CLAUDE_4_5_SONNET_THINKING` | Claude |
| 340 | `MODEL_CLAUDE_4_5_HAIKU` | Claude |
| 341 | `MODEL_CLAUDE_4_5_HAIKU_THINKING` | Claude |

### Codename Models (Unreleased / Internal)

| Enum ID | Codename | Notes |
|---------|----------|-------|
| 328 | `NEMOSREEF` | Unknown mapping |
| 336 | `HORIZONDAWN` | Unknown mapping |
| 337 | `PUREPRISM` | Unknown mapping |
| 338 | `GENTLEISLAND` | Unknown mapping |
| 339 | `RAINSONG` | Unknown mapping |
| 343 | `ORIONFIRE` | Unknown mapping |
| 347 | `COSMICFORGE` | Unknown mapping |
| 348 | `RIFTRUNNER` | Likely Gemini 3 family |
| 350 | `INFINITYJET` | Unknown mapping |
| 351 | `INFINITYBLOOM` | Unknown mapping |
| 352 | `RIFTRUNNER_THINKING_LOW` | RIFTRUNNER with low thinking budget |
| 353 | `RIFTRUNNER_THINKING_HIGH` | RIFTRUNNER with high thinking budget |

### Internal / Special Models

| Enum ID | Name | Notes |
|---------|------|-------|
| 235 | `MODEL_CHAT_20706` | Legacy |
| 269 | `MODEL_CHAT_23310` | Legacy |
| 323 | `GOOGLE_GEMINI_TRAINING_POLICY` | Training data governance |
| 326 | `GOOGLE_GEMINI_INTERNAL_BYOM` | Bring Your Own Model |
| 327 | `GOOGLE_GEMINI_FOR_GOOGLE_2_5_PRO` | Google-internal variant |
| 331 | `GOOGLE_GEMINI_2_5_PRO_EVAL` | Evaluation only |
| 332 | `GOOGLE_GEMINI_2_5_FLASH_IMAGE_PREVIEW` | Image generation |
| 335 | `GOOGLE_GEMINI_COMPUTER_USE_EXPERIMENTAL` | Computer use |
| 342 | `MODEL_OPENAI_GPT_OSS_120B_MEDIUM` | OpenAI via Vertex |
| 344 | `GOOGLE_GEMINI_INTERNAL_TAB_FLASH_LITE` | Tab completion |
| 345 | `GOOGLE_GEMINI_INTERNAL_TAB_JUMP_FLASH_LITE` | Tab jump |
| 346 | `GOOGLE_JARVIS_PROXY` | Jarvis (Google's agent?) |
| 349 | `GOOGLE_JARVIS_V4S` | Jarvis v4 |
| 1001-1011 | `MODEL_PLACEHOLDER_M1` through `M11` | Reserved slots |

**Key insight**: Codename → API string resolution happens **server-side**. The client only sends enum IDs; it never sees the actual model API strings (e.g., `gemini-3-pro-preview`).

## Model Aliases (exa.codeium_common_pb.ModelAlias)

| Enum ID | Alias | Purpose |
|---------|-------|---------|
| 1 | `CASCADE_BASE` | Default cascade model |
| 3 | `VISTA` | Unknown (vision-related?) |
| 4 | `SHAMU` | Unknown |
| 5 | `SWE_1` | Software engineering, heavy tasks |
| 6 | `SWE_1_LITE` | SWE lighter variant |
| 7 | `AUTO` | Auto-select best model |
| 8 | `RECOMMENDED` | System recommendation |

Aliases are resolved server-side to concrete models based on user tier, quota, and task type.

## API Providers (exa.codeium_common_pb.APIProvider)

| Enum ID | Provider | Description |
|---------|----------|-------------|
| 1 | `INTERNAL` | Google internal infrastructure |
| 3 | `GOOGLE_VERTEX` | Vertex AI (Gemini native) |
| 24 | `GOOGLE_GEMINI` | Gemini API (generativelanguage.googleapis.com) |
| 26 | `ANTHROPIC_VERTEX` | Claude via Vertex AI — **this is what gcli2api's Antigravity route uses** |
| 30 | `GOOGLE_EVERGREEN` | Evergreen (long-lived model versions?) |
| 31 | `OPENAI_VERTEX` | OpenAI models via Vertex AI |

## Model Providers (exa.codeium_common_pb.ModelProvider)

| Enum ID | Provider |
|---------|----------|
| 1 | `ANTIGRAVITY` |
| 2 | `OPENAI` |
| 3 | `ANTHROPIC` |
| 4 | `GOOGLE` |
| 5 | `XAI` |
| 6 | `DEEPSEEK` |

## Pricing Types (exa.codeium_common_pb.ModelPricingType)

| Enum ID | Type | Description |
|---------|------|-------------|
| 1 | `STATIC_CREDIT` | Fixed credit cost per request |
| 2 | `API` | Usage-based API pricing |
| 3 | `BYOK` | Bring Your Own Key |

## Subscription Tiers (exa.codeium_common_pb.TeamsTier)

| Enum ID | Tier | Description |
|---------|------|-------------|
| 0 | `UNSPECIFIED` | Free tier (individual) |
| 1 | `TEAMS` | Teams plan |
| 2 | `PRO` | Pro plan |
| 3 | `ENTERPRISE_SAAS` | Enterprise SaaS |
| 4 | `HYBRID` | Hybrid deployment |
| 5 | `ENTERPRISE_SELF_HOSTED` | Self-hosted enterprise |
| 6 | `WAITLIST_PRO` | Pro waitlist |
| 7 | `TEAMS_ULTIMATE` | Teams Ultimate |
| 8 | `PRO_ULTIMATE` | Pro Ultimate |
| 9 | `TRIAL` | Trial |
| 10 | `ENTERPRISE_SELF_SERVE` | Enterprise self-serve |

## Upgrade Types (UserTier.UpgradeType)

| Enum ID | Type | Notes |
|---------|------|-------|
| 1 | `GDP` | Google Developer Program |
| 2 | `GOOGLE_ONE` | Google One AI Premium |
| 3 | `GDP_HELIUM` | GDP with Helium features |
| 4 | `GOOGLE_ONE_HELIUM` | Google One with Helium |

## ClientModelConfig Fields

Each model exposed to the UI has a `ClientModelConfig`:

```protobuf
message ClientModelConfig {
  string label = 1;               // Display name (e.g., "Claude 4 Opus")
  ModelOrAlias model_or_alias = 2; // Model enum or alias
  float credit_multiplier = 3;    // Cost multiplier (e.g., 1.0, 2.0, 5.0)
  bool disabled = 4;
  bool supports_images = 5;
  bool supports_legacy = 6;
  bool is_premium = 7;            // Requires paid tier
  string beta_warning_message = 8;
  bool is_beta = 9;
  ModelProvider provider = 10;
  bool is_recommended = 11;
  repeated TeamsTier allowed_tiers = 12;  // Which tiers can use this
  ModelPricingType pricing_type = 13;
  string description = 14;
  QuotaInfo quota_info = 15;      // Current quota state
  string tag_title = 16;
  string tag_description = 17;
  map<string, bool> supported_mime_types = 18;
}
```

## ModelInfo Fields (Internal Model Metadata)

```protobuf
message ModelInfo {
  Model model_id = 1;
  bool is_internal = 2;
  ModelType model_type = 3;
  int32 max_tokens = 4;
  string tokenizer_type = 5;
  ModelFeatures model_features = 6;
  APIProvider api_provider = 7;
  string model_name = 8;           // API model string (e.g., "gemini-2.5-flash")
  bool supports_context = 9;
  int32 embed_dim = 10;
  string base_url = 11;
  string chat_model_name = 12;
  int32 max_output_tokens = 13;
  PromptTemplaterType prompt_templater_type = 14;
  ToolFormatterType tool_formatter_type = 15;
  int32 thinking_budget = 16;
  int32 min_thinking_budget = 17;
}
```

## gcli2api Relevance

### Already implemented
- Dynamic model list via `v1internal:fetchAvailableModels`
- Model alias routing in `GEMINICLI_MODEL_ALIASES` and `ANTIGRAVITY_MODEL_ALIASES`
- Antigravity version spoofing in User-Agent

### Potential improvements
- **Use codename enum IDs** instead of string model names for upstream requests (more robust against name changes)
- **Track `credit_multiplier`** per model to predict quota consumption
- **Monitor `is_premium` / `allowed_tiers`** to detect tier changes that affect access
- **Probe codename models** (RIFTRUNNER, COSMICFORGE, etc.) against the live API to discover their capabilities

## See Also

- [antigravity-quota.md](antigravity-quota.md) — Credits system and quota tracking
- [antigravity-protocol.md](antigravity-protocol.md) — Endpoints and transport
