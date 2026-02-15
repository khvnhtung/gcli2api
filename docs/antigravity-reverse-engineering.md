# Antigravity Reverse Engineering - Methodology

> How to extract protocol details from the Antigravity Linux app.
> Last updated: 2026-02-15 | Antigravity v1.107.0 (build 1504c8cc4b)

## What Is Antigravity

Antigravity is Google's **VS Code fork** (Electron/Chromium) for AI-assisted coding.
Internally it uses **ConnectRPC** (gRPC-web over HTTP/2) with **Protobuf** (`@bufbuild/protobuf` + `@connectrpc/connect`).
The proto definitions ship as bundled JS in the app — no `.proto` files, but message/service structures are extractable from minified code.

## Installation

```bash
# Add repo
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://us-central1-apt.pkg.dev/doc/repo-signing-key.gpg | \
  sudo gpg --dearmor --yes -o /etc/apt/keyrings/antigravity-repo-key.gpg
echo "deb [signed-by=/etc/apt/keyrings/antigravity-repo-key.gpg] \
  https://us-central1-apt.pkg.dev/projects/antigravity-auto-updater-dev/ \
  antigravity-debian main" | sudo tee /etc/apt/sources.list.d/antigravity.list > /dev/null

# Install
sudo apt update && sudo apt install antigravity -y

# Verify
antigravity --version
dpkg -L antigravity | head -20
```

## Key Files

| File | Size | Description |
|------|------|-------------|
| `/usr/bin/antigravity` | symlink | → `/usr/share/antigravity/bin/antigravity` (shell launcher) |
| `/usr/share/antigravity/antigravity` | ~150MB | Electron binary |
| `/usr/share/antigravity/resources/app/package.json` | 2KB | App metadata, dependency list |
| `/usr/share/antigravity/resources/app/out/main.js` | 5.5MB | **Main process** — auth, transport, ConnectRPC setup |
| `/usr/share/antigravity/resources/app/out/jetskiAgent/main.js` | 8.7MB | **Jetski AI agent** — cascade logic, model handling |
| `/usr/share/antigravity/resources/app/out/vs/workbench/workbench.desktop.main.js` | large | **Workbench** — UI, editor integration |
| `/usr/share/antigravity/resources/app/out/cli.js` | 208KB | CLI entrypoint |
| `/usr/share/antigravity/resources/app/node_modules.asar` | packed | Node modules (extractable with `npx asar extract`) |

## Extraction Techniques

### 1. Extract URLs (endpoints, OAuth, telemetry)

```bash
grep -oP 'https?://[a-zA-Z0-9._/-]+' \
  /usr/share/antigravity/resources/app/out/main.js | sort -u
```

### 2. Extract Proto Namespaces and Message Names

```bash
# Find all exa.* proto types
grep -oP 'exa\.\w+_pb\.\w+' \
  /usr/share/antigravity/resources/app/out/main.js | sort -u

# Find google.internal.* types
grep -oP 'google\.internal\.\w+[\.\w]*' \
  /usr/share/antigravity/resources/app/out/main.js | sort -u
```

### 3. Extract ConnectRPC Service Definitions

```bash
# Find service type names
grep -oP '(Service|ServiceType)\s*=\s*\{[^}]+\}' \
  /usr/share/antigravity/resources/app/out/main.js

# Find RPC method definitions (look for kind: "unary" or "server_streaming")
grep -oP '"(unary|server_streaming|client_streaming|bidi_streaming)"' \
  /usr/share/antigravity/resources/app/out/main.js
```

### 4. Extract Proto Message Fields

The minified JS uses `proto3.makeMessageType()`. Search for field arrays:
```bash
# Find proto field definitions (they look like arrays of field descriptors)
# Each field: {no: N, name: "field_name", kind: "scalar/message/enum", T: type, ...}
grep -oP '\{no:\d+,name:"[^"]+",kind:"[^"]+"' \
  /usr/share/antigravity/resources/app/out/main.js
```

### 5. Extract Enum Values

```bash
# Proto enums are defined as objects with numeric values
grep -oP '\{[A-Z_]+:\d+' \
  /usr/share/antigravity/resources/app/out/main.js | head -50
```

### 6. Extract the ASAR Bundle (for node_modules inspection)

```bash
mkdir -p /tmp/antigravity-extract
npx asar extract \
  /usr/share/antigravity/resources/app/node_modules.asar \
  /tmp/antigravity-extract
```

### 7. Find Auth Configuration

```bash
# OAuth client IDs
grep -oP '\d{10,}-[a-z0-9]+\.apps\.googleusercontent\.com' \
  /usr/share/antigravity/resources/app/out/main.js

# API keys
grep -oP 'AIzaSy[a-zA-Z0-9_-]{33}' \
  /usr/share/antigravity/resources/app/out/main.js

# Scopes
grep -oP 'googleapis\.com/auth/[a-z._-]+' \
  /usr/share/antigravity/resources/app/out/main.js
```

## Proto Source Paths (Embedded in Bundle)

These paths appear in the minified JS as source references:

```
exa/proto_ts/out/dist/exa/agent_manager_pb/agent_manager_pb.js
exa/proto_ts/out/dist/exa/api_server_pb/api_server_pb.js
exa/proto_ts/out/dist/exa/browser_pb/browser_pb.js
exa/proto_ts/out/dist/exa/cascade_plugins_pb/cascade_plugins_pb.js
exa/proto_ts/out/dist/exa/chat_client_server_pb/chat_client_server_pb.js
exa/proto_ts/out/dist/exa/chat_pb/chat_pb.js
exa/proto_ts/out/dist/exa/code_edit/code_edit_pb/code_edit_pb.js
exa/proto_ts/out/dist/exa/codeium_common_pb/codeium_common_pb.js
exa/proto_ts/out/dist/exa/context_module_pb/context_module_pb.js
exa/proto_ts/out/dist/exa/cortex_pb/cortex_pb.js
exa/proto_ts/out/dist/exa/diff_action_pb/diff_action_pb.js
exa/proto_ts/out/dist/exa/gemini_coder/proto/trajectory_pb.js
exa/proto_ts/out/dist/exa/google/internal/cloud/code/v1internal/credits_pb.js
exa/proto_ts/out/dist/exa/google/internal/cloud/code/v1internal/jetski_service_pb.js
exa/proto_ts/out/dist/exa/google/internal/cloud/code/v1internal/metrics_pb.js
exa/proto_ts/out/dist/exa/google/internal/cloud/code/v1internal/model_configs_pb.js
exa/proto_ts/out/dist/exa/google/internal/cloud/code/v1internal/onboarding_pb.js
exa/proto_ts/out/dist/exa/index_pb/index_pb.js
exa/proto_ts/out/dist/exa/jetski_cortex_pb/jetski_cortex_pb.js
exa/proto_ts/out/dist/exa/language_server_pb/language_server_pb.js
exa/proto_ts/out/dist/exa/opensearch_clients_pb/opensearch_clients_pb.js
exa/proto_ts/out/dist/exa/prompt_pb/prompt_pb.js
exa/proto_ts/out/dist/exa/unified_state_sync_pb/unified_state_sync_pb.js
```

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│ Antigravity (Electron App)                              │
│                                                         │
│  ┌──────────┐   ┌────────────┐   ┌──────────────────┐  │
│  │ main.js  │──►│ Sidecar    │──►│ Language Server   │  │
│  │ (renderer│   │ (local     │   │ (cloud)           │  │
│  │  process)│   │  HTTP/2)   │   │                   │  │
│  └──────────┘   └────────────┘   └──────────────────┘  │
│       │              │                    │              │
│       ▼              ▼                    ▼              │
│  ConnectRPC     127.0.0.1:PORT    googleapis.com        │
└─────────────────────────────────────────────────────────┘

Cloud endpoints:
  Production: https://daily-cloudcode-pa.googleapis.com
  Sandbox:    https://autopush-cloudcode-pa.sandbox.googleapis.com

Local sidecar:
  http://127.0.0.1:{dynamic_port}  (ConnectRPC HTTP/2)

Services:
  exa.language_server_pb.LanguageServerService  (120 RPCs)
  antigravity.sidecar.v1.SidecarService         (2 RPCs)
```

## Updating After New Releases

```bash
sudo apt update && sudo apt install --only-upgrade antigravity
antigravity --version
# Then re-run extraction commands above
```

## See Also

- [antigravity-protocol.md](antigravity-protocol.md) — Endpoints, auth, transport details
- [antigravity-models.md](antigravity-models.md) — Model enums, codenames, aliases
- [antigravity-cascade-flow.md](antigravity-cascade-flow.md) — How AI chat messages flow
- [antigravity-quota.md](antigravity-quota.md) — Credits, quota, billing
- [antigravity-gcli2api-gaps.md](antigravity-gcli2api-gaps.md) — Actionable improvements for gcli2api
