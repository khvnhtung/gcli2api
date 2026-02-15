# Antigravity Protocol Details

> Endpoints, authentication, transport, and headers.
> Extracted from Antigravity v1.107.0 on 2026-02-15.

## Endpoints

| Name | URL | Purpose |
|------|-----|---------|
| **Production** | `https://daily-cloudcode-pa.googleapis.com` | Main API (all RPCs) |
| **Sandbox** | `https://autopush-cloudcode-pa.sandbox.googleapis.com` | Dev/staging |
| **User Info** | `https://www.googleapis.com/oauth2/v2/userinfo` | Get user profile |
| **Telemetry** | `https://play.googleapis.com/log` | Usage logging |
| **Feedback** | `https://feedback-pa.googleapis.com/v1/products/5372208/web:submit` | Bug reports |
| **Auto-update** | `https://antigravity-auto-updater-974169037036.us-central1.run.app/api/update/` | Version check |
| **Insider update** | `https://antigravity-auto-updater-insider-974169037036.us-central1.run.app/api/update/` | Insider builds |
| **Insider secret** | `https://dl.google.com/antigravity/insider_secret` | Insider access key |
| **Auth callback** | `https://antigravity.google/auth-success` | OAuth redirect |
| **Local sidecar** | `http://127.0.0.1:{dynamic_port}` | Editor ↔ sidecar IPC |

## Authentication

### OAuth2 Configuration

| Field | Value |
|-------|-------|
| **Client ID** | `1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com` |
| **Feedback API Key** | `AIzaSyCNwssj18yx5z0YgvDoBCiewnY_xSXyaWk` |
| **Feedback Product ID** | `5372208` |

### Scopes

```
https://www.googleapis.com/auth/cloud-platform
https://www.googleapis.com/auth/userinfo.email
https://www.googleapis.com/auth/userinfo.profile
https://www.googleapis.com/auth/cclog
https://www.googleapis.com/auth/experimentsandconfigs
```

### How Auth Works

1. User signs in via browser OAuth flow → redirect to `https://antigravity.google/auth-success`
2. App receives OAuth tokens (access_token + refresh_token)
3. Tokens stored in local keychain/credential store
4. On each RPC call, access token is attached as `Authorization: Bearer <token>`
5. Token refresh happens automatically when expired

### gcli2api comparison

gcli2api uses a **different OAuth client ID**: `681255809395-...` (GeminiCLI's). The Antigravity client ID is `1071006060591-...`. Both work for the same APIs but may have different rate limit buckets.

## Transport Layer

### ConnectRPC v2

- **Protocol**: ConnectRPC (gRPC-web compatible) over **HTTP/2**
- **Library**: `@connectrpc/connect` + `@connectrpc/connect-node`
- **Serialization**: Protocol Buffers (binary, not JSON)
- **Client creation**: Uses `createClient()` (ConnectRPC v2 API, not the older `createPromiseClient`)

### Request Format

```
POST https://daily-cloudcode-pa.googleapis.com/exa.language_server_pb.LanguageServerService/StartCascade
Content-Type: application/proto
Connect-Protocol-Version: 1
Authorization: Bearer <oauth_access_token>
```

For server-streaming RPCs:
```
POST .../HandleStreamingCommand
Content-Type: application/connect+proto
Connect-Protocol-Version: 1
Connect-Content-Encoding: identity
```

### User-Agent

Format: `antigravity/<version> <os>/<arch>`

Examples:
- `antigravity/1.107.0 linux/x64`
- `antigravity/1.107.0 darwin/arm64`

gcli2api already matches this format in `src/utils.py:69-85`.

## Services

### Primary: `exa.language_server_pb.LanguageServerService`

**Full URL pattern**: `https://daily-cloudcode-pa.googleapis.com/exa.language_server_pb.LanguageServerService/{MethodName}`

120 RPC methods. Key ones for gcli2api:

| Method | Kind | Purpose |
|--------|------|---------|
| `StartCascade` | Unary | Start new AI conversation |
| `SendUserCascadeMessage` | Unary | Send follow-up message |
| `HandleStreamingCommand` | ServerStreaming | Stream AI responses |
| `StreamCascadeReactiveUpdates` | ServerStreaming | Live diff updates |
| `StreamCascadePanelReactiveUpdates` | ServerStreaming | Panel state updates |
| `StreamCascadeSummariesReactiveUpdates` | ServerStreaming | Conversation summaries |
| `CancelCascadeInvocation` | Unary | Cancel running request |
| `HandleCascadeUserInteraction` | Unary | Approve/reject tool calls |
| `GetCascadeModelConfigs` | Unary | List available models |
| `GetModelStatuses` | Unary | Model health/status |
| `GetUserStatus` | Unary | User tier, quota |
| `GetStatus` | Unary | Service health |
| `Heartbeat` | Unary | Keep-alive |
| `StreamTerminalShellCommand` | ClientStreaming | Terminal command execution |

### Local: `antigravity.sidecar.v1.SidecarService`

Runs on `http://127.0.0.1:{port}`, used for editor ↔ sidecar IPC.

| Method | Kind | Purpose |
|--------|------|---------|
| `OpenEditor` | Unary | Open file in editor |
| `EventStream` | BiDi Streaming | Bidirectional events |

## Complete RPC Method List

<details>
<summary>All 120 LanguageServerService methods (click to expand)</summary>

```
AcceptTermsOfService
AcknowledgeCascadeCodeEdit
AcknowledgeCodeActionStep
AddToBrowserWhitelist
AddTrackedWorkspace
BrowserValidateCascadeOrCancelOverlay
CancelCascadeInvocation
CancelCascadeSteps
CaptureConsoleLogs
CaptureScreenshot
ConvertTrajectoryToMarkdown
CopyBuiltinWorkflowToWorkspace
CopyTrajectory
CreateCustomizationFile
CreateReplayWorkspace
CreateTrajectoryShare
CreateWorktree
DeleteCascadeMemory
DeleteCascadeTrajectory
DeleteMediaArtifact
DeleteQueuedUserInputStep
DumpFlightRecorder
DumpPprof
Exit
FocusUserPage
ForceBackgroundResearchRefresh
GenerateCommitMessage
GetAgentScripts
GetAllBrowserWhitelistedUrls
GetAllCascadeTrajectories
GetAllCustomAgentConfigs
GetAllRules
GetAllSkills
GetAllWorkflows
GetArtifactSnapshots
GetAvailableCascadePlugins
GetBrowserOpenConversation
GetBrowserWhitelistFilePath
GetCascadeMemories
GetCascadeModelConfigData
GetCascadeModelConfigs
GetCascadeNuxes
GetCascadePluginById
GetCascadeTrajectory
GetCascadeTrajectoryGeneratorMetadata
GetCascadeTrajectorySteps
GetChangelog
GetCodeValidationStates
GetCommandModelConfigs
GetDebugDiagnostics
GetMatchingContextScopeItems
GetMcpServerStates
GetMcpServerTemplates
GetModelResponse
GetModelStatuses
GetPatchAndCodeChange
GetProfileData
GetRepoInfos
GetRevertPreview
GetRevisionArtifact
GetStaticExperimentStatus
GetStatus
GetTeamOrganizationalControls
GetTermsOfService
GetTranscription
GetUnleashData
GetUserAnalyticsSummary
GetUserMemories
GetUserSettings
GetUserStatus
GetUserTrajectory
GetUserTrajectoryDebug
GetUserTrajectoryDescriptions
GetWebDocsOptions
GetWorkingDirectories
GetWorkspaceEditState
GetWorkspaceInfos
HandleCascadeUserInteraction
HandleScreenRecording
HandleStreamingCommand
Heartbeat
ImportFromCursor
InitializeCascadePanelState
InstallCascadePlugin
ListMcpResources
ListPages
LoadReplayConversation
LoadTrajectory
MigrateApiKey
OpenUrl
ProvideCompletionFeedback
RecordAnalyticsEvent
RecordChatFeedback
RecordChatPanelSession
RecordCommitMessageSave
RecordEvent
RecordInteractiveCascadeFeedback
RecordLints
RecordSearchDocOpen
RecordSearchResultsView
RecordUserGrep
RecordUserStepSnapshot
RefreshContextForIdeAction
RefreshMcpServers
RegisterGdmUser
RemoveTrackedWorkspace
ReplayGroundTruthTrajectory
ResetOnboarding
ResolveOutstandingSteps
RevertToCascadeStep
SaveMediaAsArtifact
SaveScreenRecording
SendActionToChatPanel
SendAllQueuedMessages
SendUserCascadeMessage
SetBaseExperiments
SetBrowserOpenConversation
SetupUniversitySandbox
SetUserSettings
SetWorkingDirectories
ShouldEnableUnleash
SignalExecutableIdle
SimulateSegFault
SkipOnboarding
SmartFocusConversation
SmartOpenBrowser
StartCascade
StartScreenRecording
StatUri
StreamCascadePanelReactiveUpdates
StreamCascadeReactiveUpdates
StreamCascadeSummariesReactiveUpdates
StreamTerminalShellCommand
StreamUserTrajectoryReactiveUpdates
UpdateCascadeMemory
UpdateConversationAnnotations
UpdateDevExperiments
UpdateEnterpriseExperimentsFromUrl
UpdatePRForWorktree
WellSupportedLanguages
```

</details>

## See Also

- [antigravity-reverse-engineering.md](antigravity-reverse-engineering.md) — Extraction methodology
- [antigravity-models.md](antigravity-models.md) — Model enums, codenames, aliases
- [antigravity-cascade-flow.md](antigravity-cascade-flow.md) — Chat message flow
- [antigravity-quota.md](antigravity-quota.md) — Credits and quota
