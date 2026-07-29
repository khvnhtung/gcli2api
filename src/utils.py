import os
import re
import platform
from datetime import datetime, timezone
from typing import List, Optional

from config import get_api_password, get_panel_password
from fastapi import Depends, HTTPException, Header, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from log import log

# HTTP Bearer security scheme
security = HTTPBearer()

# ====================== Shared Constants ======================

# Arch mapping: Python platform.machine() → Electron/Node.js-style arch string
# Used by both Antigravity and GeminiCLI UA construction
_ARCH_MAP = {
    "x86_64": "x64",
    "amd64": "x64",
    "aarch64": "arm64",
}

# ====================== OAuth Configuration ======================

# GeminiCLI User-Agent
# Real format: GeminiCLI/<version>/<model> (<platform>; <arch>)
# e.g. GeminiCLI/0.28.2/gemini-2.5-flash (linux; x64)
# Version is fetched from npm registry at startup; model is added per-request.
_GEMINICLI_VERSION: Optional[str] = None


def _fetch_geminicli_version() -> str:
    """Fetch latest GeminiCLI version from npm registry."""
    global _GEMINICLI_VERSION
    if _GEMINICLI_VERSION:
        return _GEMINICLI_VERSION

    fallback = "0.28.2"
    try:
        import requests
        resp = requests.get(
            "https://registry.npmjs.org/@google/gemini-cli/latest",
            timeout=5,
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 200:
            version = resp.json().get("version")
            if version:
                _GEMINICLI_VERSION = version
                log.info(f"Fetched GeminiCLI version {version} from npm")
                return version
    except Exception as e:
        log.debug(f"Failed to fetch GeminiCLI version from npm: {e}")

    log.warning(f"Using fallback GeminiCLI version: {fallback}")
    _GEMINICLI_VERSION = fallback
    return fallback


def _get_node_platform_arch() -> tuple:
    """Get platform/arch strings matching Node.js process.platform / process.arch."""
    os_name = platform.system().lower()
    arch = platform.machine().lower()
    # Node.js uses 'x64' not 'x86_64', 'arm64' not 'aarch64'
    arch = _ARCH_MAP.get(arch, arch)
    return os_name, arch


def get_geminicli_user_agent(model: str = "") -> str:
    """Build GeminiCLI User-Agent string.

    Format: GeminiCLI/<version>/<model> (<platform>; <arch>)
    Example: GeminiCLI/0.28.2/gemini-2.5-flash (linux; x64)
    """
    env_override = os.getenv("GEMINICLI_USER_AGENT")
    if env_override:
        return env_override

    version = _fetch_geminicli_version()
    os_name, arch = _get_node_platform_arch()
    model_part = f"/{model}" if model else ""
    return f"GeminiCLI/{version}{model_part} ({os_name}; {arch})"


# Static default for non-API uses (auth, web panel verification)
GEMINICLI_USER_AGENT = get_geminicli_user_agent()

# ====================== Dynamic Antigravity Version ======================
# Google-side Antigravity endpoints enforce a minimum client version.
# We dynamically fetch the latest version from Google's auto-updater API.
# The API path format is: /api/update/{platform}-{arch}/stable/{current_version}
# It returns JSON with a "productVersion" field containing the latest client version.

# Platform-arch combos that the updater API accepts (same as Electron update paths)
_UPDATER_PLATFORM_MAP = {
    ("linux", "x86_64"): "linux-x64",
    ("linux", "aarch64"): "linux-arm64",
    ("darwin", "x86_64"): "darwin-x64",
    ("darwin", "arm64"): "darwin-arm64",
}

VERSION_BASE_URL = "https://antigravity-auto-updater-974169037036.us-central1.run.app"
FALLBACK_VERSION = "1.107.0"  # Keep reasonably current; updated 2026-02-15

# Cache the fetched version to avoid repeated requests
_cached_antigravity_version: Optional[str] = None


def _get_updater_platform() -> str:
    """Map current OS/arch to the updater API's platform-arch string."""
    os_name = platform.system().lower()
    arch = platform.machine().lower()
    return _UPDATER_PLATFORM_MAP.get((os_name, arch), "linux-x64")


def _fetch_antigravity_version() -> str:
    """Fetch the latest Antigravity version from Google's updater API.

    Calls the proper update check endpoint:
        GET /api/update/{platform}/stable/{current_version}
    and extracts "productVersion" from the JSON response.
    """
    global _cached_antigravity_version

    # Return cached version if available
    if _cached_antigravity_version:
        return _cached_antigravity_version

    # Check for env override first
    env_override = os.getenv("ANTIGRAVITY_USER_AGENT")
    if env_override:
        log.info(f"Using ANTIGRAVITY_USER_AGENT from env: {env_override}")
        return env_override

    import requests

    plat = _get_updater_platform()
    # Use a very old version so the API always returns the latest
    url = f"{VERSION_BASE_URL}/api/update/{plat}/stable/0.0.1"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            version = data.get("productVersion")
            if version:
                _cached_antigravity_version = version
                log.info(f"Fetched Antigravity version {version} from updater API")
                return version
            log.debug(f"Updater API response missing productVersion: {list(data.keys())}")
    except Exception as e:
        log.debug(f"Failed to fetch version from updater API: {e}")

    # Fallback
    log.warning(f"Using fallback Antigravity version: {FALLBACK_VERSION}")
    _cached_antigravity_version = FALLBACK_VERSION
    return FALLBACK_VERSION


def get_antigravity_user_agent() -> str:
    """Get the Antigravity User-Agent string with dynamic version.

    Format: antigravity/<version> <os>/<arch>
    Example: antigravity/1.107.0 linux/x64
    """
    # Check for full override first
    env_override = os.getenv("ANTIGRAVITY_USER_AGENT")
    if env_override:
        return env_override

    version = _fetch_antigravity_version()
    os_name = platform.system().lower()
    arch = platform.machine().lower()
    # Normalize to Electron-style arch names (real client uses x64, not x86_64)
    arch = _ARCH_MAP.get(arch, arch)

    return f"antigravity/{version} {os_name}/{arch}"


# Initialize on module load - fetches version once and caches it
ANTIGRAVITY_USER_AGENT = get_antigravity_user_agent()

# OAuth Configuration - 标准模式
CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
CLIENT_SECRET = "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"
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Antigravity OAuth Configuration
ANTIGRAVITY_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
ANTIGRAVITY_CLIENT_SECRET = "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"
ANTIGRAVITY_SCOPES = [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/cclog',
    'https://www.googleapis.com/auth/experimentsandconfigs'
]

# 统一的 Token URL（两种模式相同）
TOKEN_URL = "https://oauth2.googleapis.com/token"

# 回调服务器配置
CALLBACK_HOST = "localhost"

# ====================== Model Configuration ======================

# Model alias mapping: short names → canonical upstream names
# This allows clients to use simpler names like "gemini-3-pro" which map to
# the actual upstream model names like "gemini-3-pro-high"
# Model aliases for GeminiCLI (/v1/) endpoint
# These map to -preview models with thinking levels
# GeminiCLI only has Gemini models, so Claude names need mapping
GEMINICLI_MODEL_ALIASES: dict[str, str] = {
    # Gemini 3.1 Pro shortcuts → default to high thinking
    "gemini-3.1-pro": "gemini-3.1-pro-high",
    # Gemini 3 Pro shortcuts → default to high thinking
    "gemini-3-pro": "gemini-3-pro-preview-high",
    "gemini-3-pro-preview": "gemini-3-pro-preview-high",
    # Gemini 3 Flash shortcuts → default to high thinking
    "gemini-3-flash": "gemini-3-flash-preview-high",
    "gemini-3-flash-preview": "gemini-3-flash-preview-high",
    # Claude model aliases (GeminiCLI doesn't have Claude, map to Gemini)
    "claude-opus-4-5-thinking": "gemini-3-pro-preview-high",
    "claude-opus-4-5": "gemini-3-pro-preview-high",
    "claude-sonnet-4-thinking": "gemini-3-flash-preview-high",
    "claude-sonnet-4": "gemini-3-flash-preview-high",
    "claude-sonnet-4-6": "gemini-3-flash-preview-high",
    "claude-haiku-4-5-20251001": "gemini-2.5-flash",
}

# Model aliases for Antigravity (/antigravity/) endpoint
# Antigravity uses different model names (no -preview suffix for flash)
# Note: claude-opus-4-5, claude-sonnet-4-5, claude-sonnet-4-6 etc. work natively on Antigravity
ANTIGRAVITY_MODEL_ALIASES: dict[str, str] = {
    # Gemini 3.1 Pro has -high/-low variants on Antigravity
    "gemini-3.1-pro": "gemini-3.1-pro-high",
    # Gemini 3 Pro has -high/-low variants on Antigravity
    "gemini-3-pro": "gemini-3-pro-high",
    "gemini-3-pro-preview": "gemini-3-pro-high",
    # Gemini 3 Flash does NOT have -high/-low on Antigravity, use base name
    # "gemini-3-flash" stays as "gemini-3-flash"
    # Defensive mapping: route Haiku to a Gemini flash tier (Haiku not available on Antigravity)
    "claude-haiku-4-5-20251001": "gemini-2.5-flash",
}

# Default aliases (used when mode is not specified)
MODEL_ALIASES: dict[str, str] = {
    # Gemini 3.1 Pro shortcuts → default to high thinking
    "gemini-3.1-pro": "gemini-3.1-pro-high",
    # Gemini 3 Pro shortcuts → default to high thinking
    "gemini-3-pro": "gemini-3-pro-high",
    "gemini-3-pro-preview": "gemini-3-pro-preview-high",
    # Gemini 3 Flash shortcuts → default to high thinking
    "gemini-3-flash-preview": "gemini-3-flash-preview-high",
    # Claude model aliases (for OpenCode compatibility)
    "claude-opus-4-5-thinking": "gemini-3-pro-high",
    "claude-opus-4-5": "gemini-3-pro-high",
    "claude-sonnet-4-thinking": "gemini-3-flash",
    "claude-sonnet-4": "gemini-3-flash",
    "claude-sonnet-4-6": "gemini-3-flash",
    # Defensive mapping: route Haiku to a Gemini flash tier.
    "claude-haiku-4-5-20251001": "gemini-2.5-flash",
}


def apply_model_alias(model_name: str, mode: str = "") -> str:
    """
    Apply model alias mapping to convert short/friendly names to canonical upstream names.

    This function:
    1. Preserves feature prefixes (假流式/, 流式抗截断/)
    2. Preserves thinking suffixes (-high, -low, -medium, etc.)
    3. Only maps base model names that have explicit aliases
    4. Uses mode-specific aliases when mode is specified

    Args:
        model_name: The model name from the client request
        mode: The API mode ("antigravity", "geminicli", or "" for default)

    Returns:
        The canonical upstream model name
    """
    if not model_name:
        return model_name

    # Select the appropriate alias map based on mode
    if mode == "antigravity":
        alias_map = ANTIGRAVITY_MODEL_ALIASES
    elif mode == "geminicli":
        alias_map = GEMINICLI_MODEL_ALIASES
    else:
        alias_map = MODEL_ALIASES

    # Extract feature prefix if present
    prefix = ""
    base_name = model_name
    for feat_prefix in ["假流式/", "流式抗截断/"]:
        if model_name.startswith(feat_prefix):
            prefix = feat_prefix
            base_name = model_name[len(feat_prefix):]
            break

    # Check if the base name (without prefix) has an alias
    if base_name in alias_map:
        mapped = alias_map[base_name]
        result = f"{prefix}{mapped}" if prefix else mapped
        log.debug(f"[MODEL ALIAS] Mapped '{model_name}' → '{result}' (mode={mode})")
        return result

    # No alias found, return original
    return model_name


# Default Safety Settings for Google API
DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_HATE", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_JAILBREAK", "threshold": "BLOCK_NONE"},
]

# Model name lists for different features
BASE_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.1-pro",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview"
]


# ====================== Model Helper Functions ======================

def is_fake_streaming_model(model_name: str) -> bool:
    """Check if model name indicates fake streaming should be used."""
    return model_name.startswith("假流式/")


def is_anti_truncation_model(model_name: str) -> bool:
    """Check if model name indicates anti-truncation should be used."""
    return model_name.startswith("流式抗截断/")


def get_base_model_from_feature_model(model_name: str) -> str:
    """Get base model name from feature model name."""
    # Remove feature prefixes
    for prefix in ["假流式/", "流式抗截断/"]:
        if model_name.startswith(prefix):
            return model_name[len(prefix) :]
    return model_name


def get_available_models(router_type: str = "openai") -> List[str]:
    """
    Get available models with feature prefixes.

    Args:
        router_type: "openai" or "gemini"

    Returns:
        List of model names with feature prefixes
    """
    models = []

    for base_model in BASE_MODELS:
        # 基础模型
        models.append(base_model)

        # 假流式模型 (前缀格式)
        models.append(f"假流式/{base_model}")

        # 流式抗截断模型 (仅在流式传输时有效，前缀格式)
        models.append(f"流式抗截断/{base_model}")

        # 定义思考后缀（根据模型系列不同）
        thinking_suffixes = []

        # Gemini 2.5 系列: 使用思考预算后缀
        if "gemini-2.5" in base_model:
            thinking_suffixes = ["-max", "-high", "-medium", "-low", "-minimal"]
        # Gemini 3 系列: 使用思考等级后缀
        elif "gemini-3" in base_model:
            if "flash" in base_model:
                # 3-flash-preview: 支持 high/medium/low/minimal
                thinking_suffixes = ["-high", "-medium", "-low", "-minimal"]
            elif "pro" in base_model:
                # 3-pro-preview: 支持 high/low
                thinking_suffixes = ["-high", "-low"]

        search_suffix = "-search"

        # 1. 单独的 thinking 后缀
        for thinking_suffix in thinking_suffixes:
            models.append(f"{base_model}{thinking_suffix}")
            models.append(f"假流式/{base_model}{thinking_suffix}")
            models.append(f"流式抗截断/{base_model}{thinking_suffix}")

        # 2. 单独的 search 后缀
        models.append(f"{base_model}{search_suffix}")
        models.append(f"假流式/{base_model}{search_suffix}")
        models.append(f"流式抗截断/{base_model}{search_suffix}")

        # 3. thinking + search 组合后缀
        for thinking_suffix in thinking_suffixes:
            combined_suffix = f"{thinking_suffix}{search_suffix}"
            models.append(f"{base_model}{combined_suffix}")
            models.append(f"假流式/{base_model}{combined_suffix}")
            models.append(f"流式抗截断/{base_model}{combined_suffix}")

    return models


# ====================== Authentication Functions ======================

async def authenticate_flexible(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    access_token: Optional[str] = Header(None, alias="access_token"),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    x_anthropic_auth_token: Optional[str] = Header(None, alias="x-anthropic-auth-token"),
    anthropic_auth_token: Optional[str] = Header(None, alias="anthropic-auth-token"),
    key: Optional[str] = Query(None)
) -> str:
    """
    统一的灵活认证函数，支持多种认证方式

    此函数可以直接用作 FastAPI 的 Depends 依赖

    支持的认证方式:
        - URL 参数: key
        - HTTP 头部: Authorization (Bearer token)
        - HTTP 头部: x-api-key
        - HTTP 头部: access_token
        - HTTP 头部: x-goog-api-key
        - HTTP 头部: x-anthropic-auth-token
        - HTTP 头部: anthropic-auth-token

    Args:
        request: FastAPI Request 对象
        authorization: Authorization 头部值（自动注入）
        x_api_key: x-api-key 头部值（自动注入）
        access_token: access_token 头部值（自动注入）
        x_goog_api_key: x-goog-api-key 头部值（自动注入）
        x_anthropic_auth_token: x-anthropic-auth-token 头部值（自动注入）
        anthropic_auth_token: anthropic-auth-token 头部值（自动注入）
        key: URL 参数 key（自动注入）

    Returns:
        验证通过的token

    Raises:
        HTTPException: 认证失败时抛出异常

    使用示例:
        @router.post("/endpoint")
        async def endpoint(token: str = Depends(authenticate_flexible)):
            # token 已验证通过
            pass
    """
    password = await get_api_password()
    token = None
    auth_method = None

    # 1. 尝试从 URL 参数 key 获取（Google 官方标准方式）
    if key:
        token = key
        auth_method = "URL parameter 'key'"

    # 2. 尝试从 x-goog-api-key 头部获取（Google API 标准方式）
    elif x_goog_api_key:
        token = x_goog_api_key
        auth_method = "x-goog-api-key header"

    # 3. 尝试从 x-anthropic-auth-token 头部获取（Anthropic 标准方式）
    elif x_anthropic_auth_token:
        token = x_anthropic_auth_token
        auth_method = "x-anthropic-auth-token header"

    # 4. 尝试从 anthropic-auth-token 头部获取（Anthropic 替代方式）
    elif anthropic_auth_token:
        token = anthropic_auth_token
        auth_method = "anthropic-auth-token header"

    # 5. 尝试从 x-api-key 头部获取
    elif x_api_key:
        token = x_api_key
        auth_method = "x-api-key header"

    # 6. 尝试从 access_token 头部获取
    elif access_token:
        token = access_token
        auth_method = "access_token header"

    # 7. 尝试从 Authorization 头部获取
    elif authorization:
        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme. Use 'Bearer <token>'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization[7:]  # 移除 "Bearer " 前缀
        auth_method = "Authorization Bearer header"

    # 检查是否提供了任何认证凭据
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials. Use 'key' URL parameter, 'x-goog-api-key', 'x-anthropic-auth-token', 'anthropic-auth-token', 'x-api-key', 'access_token' header, or 'Authorization: Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 验证 token
    if token != password:
        log.debug(f"Authentication failed using {auth_method}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="密码错误"
        )
    
    log.debug(f"Authentication successful using {auth_method}")
    return token


# 为了保持向后兼容，保留旧函数名作为别名
authenticate_bearer = authenticate_flexible
authenticate_gemini_flexible = authenticate_flexible


# ====================== Panel Authentication Functions ======================

async def verify_panel_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    简化的控制面板密码验证函数

    直接验证Bearer token是否等于控制面板密码

    Args:
        credentials: HTTPAuthorizationCredentials 自动注入

    Returns:
        验证通过的token

    Raises:
        HTTPException: 密码错误时抛出401异常
    """

    password = await get_panel_password()
    if credentials.credentials != password:
        raise HTTPException(status_code=401, detail="密码错误")
    return credentials.credentials
