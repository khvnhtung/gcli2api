## Upstream Changes Report

| Commit | Description | Priority | Status | Notes |
|--------|-------------|----------|--------|-------|
| 93d788f | 消息测试增加详细报错信息 | LOW | Skip | UI improvement for testing credentials, not critical for backend functionality. |
| 8d91633 | 设置preview通道功能 | MEDIUM | Consider | Adds ability to configure preview channel for credentials to fix 404s on preview models. Requires frontend changes. |
| b8d00f7 | 优化筛选逻辑 | LOW | Skip | Frontend filtering logic optimization. |
| 1104079 | Update mongodb_manager.py | LOW | Skip | MongoDB specific updates, we use SQLite/File storage primarily. |
| b3a0f00 | Update utils.py | LOW | Skip | Likely minor utility updates. |

### Analysis of "设置preview通道功能" (8d91633)

This commit adds a new endpoint `/configure-preview/{filename}` to `src/panel/creds.py` (which seems to be merged into `src/web_routes.py` in our fork or located differently).

**Functionality:**
It calls the Google Cloud API to set the `release_channel` to `EXPERIMENTAL` for a specific project. This is reportedly useful for fixing 404 errors when using `preview` models (e.g., `gemini-3-pro-preview`).

**Implementation Details:**
1.  **New Endpoint:** `POST /configure-preview/{filename}`
2.  **Logic:**
    *   Validates credential mode (only `geminicli`).
    *   Refreshes token if needed.
    *   Calls `https://cloudaicompanion.googleapis.com/v1/projects/{project_id}/locations/global/releaseChannelSettings` to set `release_channel` to `EXPERIMENTAL`.
    *   Calls `.../settingBindings` to bind the setting to the project.
    *   Updates local credential state with `"preview": true`.

**Relevance to Us:**
If we plan to support or use `gemini-3-pro-preview` or other experimental models, this feature is valuable. However, it requires significant frontend changes (`front/common.js`, `front/control_panel.html`) to expose the button to the user.

**Recommendation:**
Since our current focus is on stability (fixing connection issues) and core functionality, and this feature is for *experimental* models, I recommend **skipping** this for now unless you specifically need to use preview models that are failing with 404s.

**Decision:**
I will **not** port any upstream changes at this moment as none are critical bug fixes for the core proxy functionality or connection issues we are investigating. The "preview channel" feature is a nice-to-have enhancement but not a blocker.
