# Antigravity Cascade Flow

> How AI chat messages are sent, streamed, and received.
> Extracted from Antigravity v1.107.0 on 2026-02-15.

## Overview

"Cascade" is Antigravity's name for its AI agent conversation system. It uses a **reactive streaming** architecture:

1. Client sends a request (`StartCascade` or `SendUserCascadeMessage`)
2. Server streams back state diffs via `StreamCascadeReactiveUpdates`
3. Client applies diffs to reconstruct the full `CortexRunState`
4. Tool calls require user approval via `HandleCascadeUserInteraction`

## Starting a Conversation

### RPC: `StartCascade` (Unary)

Initiates a new cascade conversation.

**Endpoint**: `POST https://daily-cloudcode-pa.googleapis.com/exa.language_server_pb.LanguageServerService/StartCascade`

**Request includes**:
- User message text
- Selected model (as `ModelOrAlias`)
- Workspace context (repo info, open files)
- Attached images/media
- Cascade config (planner model, executor model, etc.)

### RPC: `SendUserCascadeMessage` (Unary)

Sends a follow-up message in an existing conversation.

**Request includes**:
- Conversation/trajectory ID
- User message text
- Additional context attachments

## Receiving AI Responses

### RPC: `StreamCascadeReactiveUpdates` (Server Streaming)

This is the **primary channel** for receiving AI responses. Instead of sending complete messages, the server sends **protobuf diffs** that the client applies incrementally.

**Response stream**: Series of `StreamReactiveUpdatesResponse` messages containing `MessageDiff` objects.

### MessageDiff Protocol

```protobuf
message MessageDiff {
  repeated FieldDiff field_diffs = 1;
}

message FieldDiff {
  uint32 field_number = 1;
  oneof diff {
    SingularValue singular = 2;    // Set a scalar/message field
    RepeatedDiff repeated = 3;     // Modify a repeated field
    MapDiff map = 4;               // Modify a map field
  }
}

message SingularValue {
  oneof value {
    bool bool_value = 1;
    int32 int32_value = 2;
    int64 int64_value = 3;
    uint32 uint32_value = 4;
    uint64 uint64_value = 5;
    float float_value = 6;
    double double_value = 7;
    string string_value = 8;
    bytes bytes_value = 9;
    int32 enum_value = 10;
    MessageDiff message_value = 11;
  }
}

message RepeatedDiff {
  oneof operation {
    AppendElements append = 1;
    SetElement set = 2;
    RemoveElements remove = 3;
    ClearElements clear = 4;
  }
}
```

**How it works**: The server maintains a `CortexRunState` and sends diffs. The client applies these diffs to its local copy. This is more efficient than re-sending the entire state on each token.

### RPC: `HandleStreamingCommand` (Server Streaming)

Alternative streaming RPC. Used for command-style interactions (possibly terminal commands, code generation).

### RPC: `StreamCascadePanelReactiveUpdates` (Server Streaming)

Streams UI panel state updates (which panels are open, sidebar state, etc.).

### RPC: `StreamCascadeSummariesReactiveUpdates` (Server Streaming)

Streams conversation summary updates (for the sidebar conversation list).

## CortexRunState (The Conversation State)

The `CortexRunState` is the **central data structure** representing the full state of a cascade conversation. It's defined in `exa.cortex_pb` and contains:

### Steps (oneof with ~80 types)

Each step in the conversation is one of these types:

**AI Output Steps**:
- Text output (the model's response)
- Thinking blocks (internal reasoning)
- Code blocks

**Tool Call Steps**:
- File read/write
- Terminal command execution
- Browser actions
- MCP tool calls
- Search (grep, file search)

**User Interaction Steps**:
- User message
- Tool approval/rejection
- Code edit acknowledgment

**State Steps**:
- Error states
- Cancellation
- Completion markers

## Tool Approval Flow

### RPC: `HandleCascadeUserInteraction` (Unary)

When the AI wants to execute a tool (file write, terminal command, etc.), it streams a tool call step. The user must approve or reject it.

**Request includes**:
- Step ID
- Approval decision (accept/reject)
- Optional modification to the tool parameters

## Cancellation

### RPC: `CancelCascadeInvocation` (Unary)

Cancels the currently running cascade invocation.

### RPC: `CancelCascadeSteps` (Unary)

Cancels specific steps within a cascade (more granular than full cancellation).

## Model Selection

### RPC: `GetCascadeModelConfigs` (Unary)

Returns available models for the current user, including:
- `ClientModelConfig` for each model (label, credit_multiplier, is_premium, quota_info)
- `ClientModelSort` for UI grouping
- `DefaultOverrideModelConfig` for the recommended default

### RPC: `GetModelStatuses` (Unary)

Returns current model health status (availability, degradation warnings).

### Auto Model Selection

When `MODEL_ALIAS_AUTO` is selected, the server decides which concrete model to use based on:
- Task complexity
- Available quota
- Model availability
- User tier

## Session Management

### RPC: `GetCascadeTrajectory` (Unary)

Retrieves a full conversation history by trajectory ID.

### RPC: `GetAllCascadeTrajectories` (Unary)

Lists all conversations for the user.

### RPC: `GetCascadeTrajectorySteps` (Unary)

Retrieves steps for a specific trajectory.

### RPC: `CopyTrajectory` (Unary)

Duplicates a conversation.

### RPC: `DeleteCascadeTrajectory` (Unary)

Deletes a conversation.

## Memory System

### RPC: `GetCascadeMemories` (Unary)

Retrieves conversation memories (persistent context across sessions).

### RPC: `UpdateCascadeMemory` (Unary)

Updates a memory entry.

### RPC: `DeleteCascadeMemory` (Unary)

Deletes a memory entry.

### RPC: `GetUserMemories` (Unary)

Retrieves user-level memories (cross-conversation).

## Comparison with gcli2api's Approach

| Aspect | Antigravity | gcli2api |
|--------|-------------|----------|
| **Transport** | ConnectRPC (binary protobuf) | HTTP REST (JSON) |
| **Streaming** | Reactive diffs (`MessageDiff`) | SSE (text/event-stream) |
| **Model selection** | Enum IDs + server-side alias resolution | String model names |
| **Tool approval** | Dedicated RPC (`HandleCascadeUserInteraction`) | Not applicable (proxy) |
| **Conversation state** | `CortexRunState` with incremental diffs | Stateless pass-through |

gcli2api operates at a **lower level** than Cascade — it proxies the raw Gemini/Anthropic API calls rather than the higher-level Cascade protocol. The Cascade protocol adds:
- Step-based conversation tracking
- Reactive streaming with diffs
- Built-in tool approval workflow
- Memory system
- Conversation management

## Relevant JS Files

| File | What's in it |
|------|-------------|
| `main.js` | ConnectRPC transport setup, auth, service client creation |
| `jetskiAgent/main.js` | Cascade logic, step processing, model handling, CortexRunState |
| `workbench.desktop.main.js` | UI bindings, panel state, user interactions |
| `jetskiAgent.js` (in workbench) | Bridge between workbench and jetski agent |

## See Also

- [antigravity-protocol.md](antigravity-protocol.md) — Endpoints and transport
- [antigravity-models.md](antigravity-models.md) — Model enums and codenames
- [antigravity-quota.md](antigravity-quota.md) — Credits and quota
