"""
Configuration constants for the Geminicli2api proxy server.
Centralizes all configuration to avoid duplication across modules.

- 启动时加载一次配置到内存
- 修改配置时调用 reload_config() 重新从数据库加载
"""

import os
from typing import Any, Optional

# 全局配置缓存
_config_cache: dict[str, Any] = {}
_config_initialized = False

# Client Configuration

# 需要自动封禁的错误码 (默认值，可通过环境变量或配置覆盖)
AUTO_BAN_ERROR_CODES = [403]

# ====================== 环境变量映射表 ======================
# 统一维护环境变量名和配置键名的映射关系
# 格式: "环境变量名": "配置键名"
ENV_MAPPINGS = {
    "CODE_ASSIST_ENDPOINT": "code_assist_endpoint",
    "CREDENTIALS_DIR": "credentials_dir",
    "PROXY": "proxy",
    "OAUTH_PROXY_URL": "oauth_proxy_url",
    "GOOGLEAPIS_PROXY_URL": "googleapis_proxy_url",
    "RESOURCE_MANAGER_API_URL": "resource_manager_api_url",
    "SERVICE_USAGE_API_URL": "service_usage_api_url",
    "ANTIGRAVITY_API_URL": "antigravity_api_url",
    "AUTO_BAN": "auto_ban_enabled",
    "AUTO_BAN_ERROR_CODES": "auto_ban_error_codes",
    "RETRY_429_MAX_RETRIES": "retry_429_max_retries",
    "RETRY_429_ENABLED": "retry_429_enabled",
    "RETRY_429_INTERVAL": "retry_429_interval",
    "ANTI_TRUNCATION_MAX_ATTEMPTS": "anti_truncation_max_attempts",
    "ENTITLEMENT_403_MODEL_COOLDOWN_SECONDS": "entitlement_403_model_cooldown_seconds",
    "MODEL_NOT_FOUND_404_MODEL_COOLDOWN_SECONDS": "model_not_found_404_model_cooldown_seconds",
    "LONG_QUOTA_COOLDOWN_ROTATE_THRESHOLD_SECONDS": "long_quota_cooldown_rotate_threshold_seconds",
    "RETRY_ROTATE_DELAY_MS": "retry_rotate_delay_ms",
    "COMPATIBILITY_MODE": "compatibility_mode_enabled",
    "RETURN_THOUGHTS_TO_FRONTEND": "return_thoughts_to_frontend",
    "ANTIGRAVITY_STREAM2NOSTREAM": "antigravity_stream2nostream",

    # Realtime quota refresh (429 w/out explicit retry time)
    "REALTIME_QUOTA_REFRESH_ENABLED": "realtime_quota_refresh_enabled",
    "REALTIME_QUOTA_REFRESH_TIMEOUT_SECONDS": "realtime_quota_refresh_timeout_seconds",
    "REALTIME_QUOTA_REFRESH_CACHE_TTL_SECONDS": "realtime_quota_refresh_cache_ttl_seconds",
    "REALTIME_QUOTA_REFRESH_FALLBACK_TO_EARLIEST_RESET": "realtime_quota_refresh_fallback_to_earliest_reset",

    # Pool exhaustion wait (avoid failing when all creds are cooled)
    "POOL_WAIT_ENABLED": "pool_wait_enabled",
    "POOL_WAIT_MAX_SECONDS": "pool_wait_max_seconds",
    "POOL_WAIT_POLL_SECONDS": "pool_wait_poll_seconds",

    # Image offload (replace images with short descriptions)
    "IMAGE_OFFLOAD_ENABLED": "image_offload_enabled",
    "IMAGE_OFFLOAD_MODEL": "image_offload_model",
    "IMAGE_OFFLOAD_CACHE_SIZE": "image_offload_cache_size",
    "IMAGE_OFFLOAD_TIMEOUT_SECONDS": "image_offload_timeout_seconds",

    # Audit log
    "AUDIT_LOG_ENABLED": "audit_log_enabled",
    "AUDIT_RAW_ENABLED": "audit_raw_enabled",
    "AUDIT_RAW_DIR": "audit_raw_dir",
    "AUDIT_RAW_RETENTION_DAYS": "audit_raw_retention_days",
    "AUDIT_RAW_MAX_BYTES": "audit_raw_max_bytes",

    "HOST": "host",
    "PORT": "port",
    "API_PASSWORD": "api_password",
    "PANEL_PASSWORD": "panel_password",
    "PASSWORD": "password",
}


def get_env_locked_keys() -> set[str]:
    """Return config keys locked by environment variables.

    Any key with a corresponding env var present should be treated as read-only
    from the web panel.
    """
    locked: set[str] = set()
    for env_var, key in ENV_MAPPINGS.items():
        if os.getenv(env_var):
            locked.add(key)
    return locked


async def build_effective_config_for_panel() -> tuple[dict[str, Any], set[str]]:
    """Build the effective config dict used by the web control panel.

    This centralizes the config surface area so future knobs only need to be
    added here (and in the frontend), instead of being scattered across routes.
    """
    from src.storage_adapter import get_storage_adapter

    current_config: dict[str, Any] = {}

    # Core endpoints
    current_config["code_assist_endpoint"] = await get_code_assist_endpoint()
    current_config["credentials_dir"] = await get_credentials_dir()
    current_config["proxy"] = await get_proxy_config() or ""
    current_config["oauth_proxy_url"] = await get_oauth_proxy_url()
    current_config["googleapis_proxy_url"] = await get_googleapis_proxy_url()
    current_config["resource_manager_api_url"] = await get_resource_manager_api_url()
    current_config["service_usage_api_url"] = await get_service_usage_api_url()
    current_config["antigravity_api_url"] = await get_antigravity_api_url()

    # Behavior knobs
    current_config["auto_ban_enabled"] = await get_auto_ban_enabled()
    current_config["auto_ban_error_codes"] = await get_auto_ban_error_codes()
    current_config["retry_429_max_retries"] = await get_retry_429_max_retries()
    current_config["retry_429_enabled"] = await get_retry_429_enabled()
    current_config["retry_429_interval"] = await get_retry_429_interval()
    current_config["model_not_found_404_model_cooldown_seconds"] = (
        await get_model_not_found_404_model_cooldown_seconds()
    )
    current_config["anti_truncation_max_attempts"] = await get_anti_truncation_max_attempts()
    current_config["compatibility_mode_enabled"] = await get_compatibility_mode_enabled()
    current_config["return_thoughts_to_frontend"] = await get_return_thoughts_to_frontend()
    current_config["antigravity_stream2nostream"] = await get_antigravity_stream2nostream()

    # Realtime quota refresh
    current_config["realtime_quota_refresh_enabled"] = await get_realtime_quota_refresh_enabled()
    current_config["realtime_quota_refresh_timeout_seconds"] = await get_realtime_quota_refresh_timeout_seconds()
    current_config["realtime_quota_refresh_cache_ttl_seconds"] = await get_realtime_quota_refresh_cache_ttl_seconds()
    current_config["realtime_quota_refresh_fallback_to_earliest_reset"] = (
        await get_realtime_quota_refresh_fallback_to_earliest_reset()
    )

    # Pool exhaustion wait
    current_config["pool_wait_enabled"] = await get_pool_wait_enabled()
    current_config["pool_wait_max_seconds"] = await get_pool_wait_max_seconds()
    current_config["pool_wait_poll_seconds"] = await get_pool_wait_poll_seconds()

    # Image offload
    current_config["image_offload_enabled"] = await get_image_offload_enabled()
    current_config["image_offload_model"] = await get_image_offload_model()
    current_config["image_offload_cache_size"] = await get_image_offload_cache_size()
    current_config["image_offload_timeout_seconds"] = await get_image_offload_timeout_seconds()

    # Audit raw payload capture
    current_config["audit_raw_enabled"] = await get_audit_raw_enabled()
    current_config["audit_raw_dir"] = await get_audit_raw_dir()
    current_config["audit_raw_retention_days"] = await get_audit_raw_retention_days()
    current_config["audit_raw_max_bytes"] = await get_audit_raw_max_bytes()

    # Server config
    current_config["host"] = await get_server_host()
    current_config["port"] = await get_server_port()
    current_config["api_password"] = await get_api_password()
    current_config["panel_password"] = await get_panel_password()
    current_config["password"] = await get_server_password()

    # Merge stored config (don't override env-locked)
    storage_adapter = await get_storage_adapter()
    storage_config = await storage_adapter.get_all_config()
    env_locked_keys = get_env_locked_keys()
    for key, value in storage_config.items():
        if key not in env_locked_keys:
            current_config[key] = value

    return current_config, env_locked_keys


# ====================== 配置系统 ======================

async def init_config():
    """初始化配置缓存（启动时调用一次）"""
    global _config_cache, _config_initialized

    if _config_initialized:
        return

    try:
        from src.storage_adapter import get_storage_adapter
        storage_adapter = await get_storage_adapter()
        _config_cache = await storage_adapter.get_all_config()
        _config_initialized = True
    except Exception:
        # 初始化失败时使用空缓存
        _config_cache = {}
        _config_initialized = True


async def reload_config():
    """重新加载配置（修改配置后调用）"""
    global _config_cache, _config_initialized

    try:
        from src.storage_adapter import get_storage_adapter
        storage_adapter = await get_storage_adapter()

        # 如果后端支持 reload_config_cache，调用它
        backend = getattr(storage_adapter, "_backend", None)
        reload_fn = getattr(backend, "reload_config_cache", None) if backend else None
        if reload_fn:
            await reload_fn()

        # 重新加载配置缓存
        _config_cache = await storage_adapter.get_all_config()
        _config_initialized = True
    except Exception:
        pass


def _get_cached_config(key: str, default: Any = None) -> Any:
    """从内存缓存获取配置（同步）"""
    return _config_cache.get(key, default)


async def get_config_value(key: str, default: Any = None, env_var: Optional[str] = None) -> Any:
    """Get configuration value with priority: ENV > Storage > default."""
    # 确保配置已初始化
    if not _config_initialized:
        await init_config()

    # Priority 1: Environment variable
    if env_var and os.getenv(env_var):
        return os.getenv(env_var)

    # Priority 2: Memory cache
    value = _get_cached_config(key)
    if value is not None:
        return value

    return default


# Configuration getters - all async
async def get_proxy_config():
    """Get proxy configuration."""
    proxy_url = await get_config_value("proxy", env_var="PROXY")
    return proxy_url if proxy_url else None


async def get_auto_ban_enabled() -> bool:
    """Get auto ban enabled setting."""
    env_value = os.getenv("AUTO_BAN")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("auto_ban_enabled", False))


async def get_auto_ban_error_codes() -> list:
    """
    Get auto ban error codes.

    Environment variable: AUTO_BAN_ERROR_CODES (comma-separated, e.g., "400,403")
    Database config key: auto_ban_error_codes
    Default: [400, 403]
    """
    env_value = os.getenv("AUTO_BAN_ERROR_CODES")
    if env_value:
        try:
            return [int(code.strip()) for code in env_value.split(",") if code.strip()]
        except ValueError:
            pass

    codes = await get_config_value("auto_ban_error_codes")
    if codes and isinstance(codes, list):
        return codes
    return AUTO_BAN_ERROR_CODES


async def get_retry_429_max_retries() -> int:
    """Get max retries for 429 errors."""
    env_value = os.getenv("RETRY_429_MAX_RETRIES")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("retry_429_max_retries", 5))


async def get_retry_429_enabled() -> bool:
    """Get 429 retry enabled setting."""
    env_value = os.getenv("RETRY_429_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("retry_429_enabled", True))


async def get_retry_429_interval() -> float:
    """Get 429 retry interval in seconds."""
    env_value = os.getenv("RETRY_429_INTERVAL")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass

    return float(await get_config_value("retry_429_interval", 0.1))


async def get_anti_truncation_max_attempts() -> int:
    """
    Get maximum attempts for anti-truncation continuation.

    Environment variable: ANTI_TRUNCATION_MAX_ATTEMPTS
    Database config key: anti_truncation_max_attempts
    Default: 3
    """
    env_value = os.getenv("ANTI_TRUNCATION_MAX_ATTEMPTS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("anti_truncation_max_attempts", 3))


async def get_entitlement_403_model_cooldown_seconds() -> int:
    """
    Cooldown seconds to apply when a credential is denied by entitlement/licensing.

    This is used for 403 errors like:
    - SUBSCRIPTION_REQUIRED
    - "You must be a named user ... Gemini Code Assist"
    - "Your account is not eligible for Gemini Code Assist"

    Environment variable: ENTITLEMENT_403_MODEL_COOLDOWN_SECONDS
    Database config key: entitlement_403_model_cooldown_seconds
    Default: 7 days
    """
    env_value = os.getenv("ENTITLEMENT_403_MODEL_COOLDOWN_SECONDS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("entitlement_403_model_cooldown_seconds", 7 * 24 * 3600))


async def get_model_not_found_404_model_cooldown_seconds() -> int:
    """Cooldown seconds for model-scoped 404 NOT_FOUND credential failures.

    Used when upstream returns model-serving 404 (for example, credential does not
    have access to a requested model). The cooldown is applied per model_key so the
    credential can still serve other models.

    Environment variable: MODEL_NOT_FOUND_404_MODEL_COOLDOWN_SECONDS
    Database config key: model_not_found_404_model_cooldown_seconds
    Default: 3600 seconds (1 hour)
    """
    env_value = os.getenv("MODEL_NOT_FOUND_404_MODEL_COOLDOWN_SECONDS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("model_not_found_404_model_cooldown_seconds", 3600))


async def get_long_quota_cooldown_rotate_threshold_seconds() -> int:
    """
    If a parsed quota reset delay is longer than this threshold, rotate accounts immediately
    instead of waiting.

    Environment variable: LONG_QUOTA_COOLDOWN_ROTATE_THRESHOLD_SECONDS
    Database config key: long_quota_cooldown_rotate_threshold_seconds
    Default: 60 seconds
    """
    env_value = os.getenv("LONG_QUOTA_COOLDOWN_ROTATE_THRESHOLD_SECONDS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("long_quota_cooldown_rotate_threshold_seconds", 60))


async def get_retry_rotate_delay_ms() -> int:
    """
    Small delay used when rotating accounts aggressively to avoid tight loops.

    Environment variable: RETRY_ROTATE_DELAY_MS
    Database config key: retry_rotate_delay_ms
    Default: 200ms
    """
    env_value = os.getenv("RETRY_ROTATE_DELAY_MS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("retry_rotate_delay_ms", 200))


# Server Configuration
async def get_server_host() -> str:
    """
    Get server host setting.

    Environment variable: HOST
    Database config key: host
    Default: 0.0.0.0
    """
    return str(await get_config_value("host", "0.0.0.0", "HOST"))


async def get_server_port() -> int:
    """
    Get server port setting.

    Environment variable: PORT
    Database config key: port
    Default: 7861
    """
    env_value = os.getenv("PORT")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("port", 7861))


async def get_api_password() -> str:
    """
    Get API password setting for chat endpoints.

    Environment variable: API_PASSWORD
    Database config key: api_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 API_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    api_password = await get_config_value("api_password", None, "API_PASSWORD")
    if api_password is not None:
        return str(api_password)

    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))


async def get_panel_password() -> str:
    """
    Get panel password setting for web interface.

    Environment variable: PANEL_PASSWORD
    Database config key: panel_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 PANEL_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    panel_password = await get_config_value("panel_password", None, "PANEL_PASSWORD")
    if panel_password is not None:
        return str(panel_password)

    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))


async def get_server_password() -> str:
    """
    Get server password setting (deprecated, use get_api_password or get_panel_password).

    Environment variable: PASSWORD
    Database config key: password
    Default: pwd
    """
    return str(await get_config_value("password", "pwd", "PASSWORD"))


async def get_credentials_dir() -> str:
    """
    Get credentials directory setting.

    Environment variable: CREDENTIALS_DIR
    Database config key: credentials_dir
    Default: ./creds
    """
    return str(await get_config_value("credentials_dir", "./creds", "CREDENTIALS_DIR"))


async def get_code_assist_endpoint() -> str:
    """
    Get Code Assist endpoint setting.

    Environment variable: CODE_ASSIST_ENDPOINT
    Database config key: code_assist_endpoint
    Default: https://cloudcode-pa.googleapis.com
    """
    return str(
        await get_config_value(
            "code_assist_endpoint", "https://cloudcode-pa.googleapis.com", "CODE_ASSIST_ENDPOINT"
        )
    )


async def get_compatibility_mode_enabled() -> bool:
    """
    Get compatibility mode setting.

    兼容性模式：启用后所有system消息全部转换成user，停用system_instructions。
    该选项可能会降低模型理解能力，但是能避免流式空回的情况。

    Environment variable: COMPATIBILITY_MODE
    Database config key: compatibility_mode_enabled
    Default: False
    """
    env_value = os.getenv("COMPATIBILITY_MODE")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("compatibility_mode_enabled", False))


async def get_return_thoughts_to_frontend() -> bool:
    """
    Get return thoughts to frontend setting.

    控制是否将思维链返回到前端。
    启用后，思维链会在响应中返回；禁用后，思维链会在响应中被过滤掉。

    Environment variable: RETURN_THOUGHTS_TO_FRONTEND
    Database config key: return_thoughts_to_frontend
    Default: True
    """
    env_value = os.getenv("RETURN_THOUGHTS_TO_FRONTEND")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("return_thoughts_to_frontend", True))


async def get_antigravity_stream2nostream() -> bool:
    """
    Get use stream for non-stream setting.

    控制antigravity非流式请求是否使用流式API并收集为完整响应。
    启用后，非流式请求将在后端使用流式API，然后收集所有块后再返回完整响应。

    Environment variable: ANTIGRAVITY_STREAM2NOSTREAM
    Database config key: antigravity_stream2nostream
    Default: True
    """
    env_value = os.getenv("ANTIGRAVITY_STREAM2NOSTREAM")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("antigravity_stream2nostream", True))


async def get_realtime_quota_refresh_enabled() -> bool:
    """Enable realtime quota refresh to get precise reset_time for 429s."""
    env_value = os.getenv("REALTIME_QUOTA_REFRESH_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("realtime_quota_refresh_enabled", True))


async def get_realtime_quota_refresh_timeout_seconds() -> float:
    env_value = os.getenv("REALTIME_QUOTA_REFRESH_TIMEOUT_SECONDS")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass
    return float(await get_config_value("realtime_quota_refresh_timeout_seconds", 20.0))


async def get_realtime_quota_refresh_cache_ttl_seconds() -> int:
    env_value = os.getenv("REALTIME_QUOTA_REFRESH_CACHE_TTL_SECONDS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    return int(await get_config_value("realtime_quota_refresh_cache_ttl_seconds", 30))


async def get_realtime_quota_refresh_fallback_to_earliest_reset() -> bool:
    env_value = os.getenv("REALTIME_QUOTA_REFRESH_FALLBACK_TO_EARLIEST_RESET")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("realtime_quota_refresh_fallback_to_earliest_reset", True))


async def get_pool_wait_enabled() -> bool:
    env_value = os.getenv("POOL_WAIT_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("pool_wait_enabled", True))


async def get_pool_wait_max_seconds() -> float:
    env_value = os.getenv("POOL_WAIT_MAX_SECONDS")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass
    return float(await get_config_value("pool_wait_max_seconds", 60.0))


async def get_pool_wait_poll_seconds() -> float:
    env_value = os.getenv("POOL_WAIT_POLL_SECONDS")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass
    return float(await get_config_value("pool_wait_poll_seconds", 1.0))


async def get_audit_raw_enabled() -> bool:
    env_value = os.getenv("AUDIT_RAW_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("audit_raw_enabled", False))


async def get_image_offload_enabled() -> bool:
    env_value = os.getenv("IMAGE_OFFLOAD_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("image_offload_enabled", False))


async def get_image_offload_model() -> str:
    return str(await get_config_value("image_offload_model", "gemini-3-flash", "IMAGE_OFFLOAD_MODEL"))


async def get_image_offload_cache_size() -> int:
    env_value = os.getenv("IMAGE_OFFLOAD_CACHE_SIZE")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    return int(await get_config_value("image_offload_cache_size", 256))


async def get_image_offload_timeout_seconds() -> float:
    env_value = os.getenv("IMAGE_OFFLOAD_TIMEOUT_SECONDS")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass
    return float(await get_config_value("image_offload_timeout_seconds", 8.0))


async def get_audit_raw_dir() -> str:
    return str(await get_config_value("audit_raw_dir", "./audit_payloads", "AUDIT_RAW_DIR"))


async def get_audit_raw_retention_days() -> int:
    env_value = os.getenv("AUDIT_RAW_RETENTION_DAYS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    return int(await get_config_value("audit_raw_retention_days", 7))


async def get_audit_raw_max_bytes() -> int:
    env_value = os.getenv("AUDIT_RAW_MAX_BYTES")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    return int(await get_config_value("audit_raw_max_bytes", 1024 * 1024))


async def get_oauth_proxy_url() -> str:
    """
    Get OAuth proxy URL setting.

    用于Google OAuth2认证的代理URL。

    Environment variable: OAUTH_PROXY_URL
    Database config key: oauth_proxy_url
    Default: https://oauth2.googleapis.com
    """
    return str(
        await get_config_value(
            "oauth_proxy_url", "https://oauth2.googleapis.com", "OAUTH_PROXY_URL"
        )
    )


async def get_googleapis_proxy_url() -> str:
    """
    Get Google APIs proxy URL setting.

    用于Google APIs调用的代理URL。

    Environment variable: GOOGLEAPIS_PROXY_URL
    Database config key: googleapis_proxy_url
    Default: https://www.googleapis.com
    """
    return str(
        await get_config_value(
            "googleapis_proxy_url", "https://www.googleapis.com", "GOOGLEAPIS_PROXY_URL"
        )
    )


async def get_resource_manager_api_url() -> str:
    """
    Get Google Cloud Resource Manager API URL setting.

    用于Google Cloud Resource Manager API的URL。

    Environment variable: RESOURCE_MANAGER_API_URL
    Database config key: resource_manager_api_url
    Default: https://cloudresourcemanager.googleapis.com
    """
    return str(
        await get_config_value(
            "resource_manager_api_url",
            "https://cloudresourcemanager.googleapis.com",
            "RESOURCE_MANAGER_API_URL",
        )
    )


async def get_service_usage_api_url() -> str:
    """
    Get Google Cloud Service Usage API URL setting.

    用于Google Cloud Service Usage API的URL。

    Environment variable: SERVICE_USAGE_API_URL
    Database config key: service_usage_api_url
    Default: https://serviceusage.googleapis.com
    """
    return str(
        await get_config_value(
            "service_usage_api_url", "https://serviceusage.googleapis.com", "SERVICE_USAGE_API_URL"
        )
    )


async def get_antigravity_api_url() -> str:
    """
    Get Antigravity API URL setting.

    用于Google Antigravity API的URL。

    Environment variable: ANTIGRAVITY_API_URL
    Database config key: antigravity_api_url
    Default: https://daily-cloudcode-pa.sandbox.googleapis.com
    """
    return str(
        await get_config_value(
            "antigravity_api_url",
            "https://daily-cloudcode-pa.sandbox.googleapis.com",
            "ANTIGRAVITY_API_URL",
        )
    )


# Cloud Code v1internal endpoints (fallback order: Sandbox → Daily → Prod)
# Matches Antigravity-Manager's multi-endpoint fallback strategy.
ANTIGRAVITY_ENDPOINT_FALLBACKS = [
    "https://daily-cloudcode-pa.sandbox.googleapis.com",  # Sandbox (default)
    "https://daily-cloudcode-pa.googleapis.com",           # Daily
    "https://cloudcode-pa.googleapis.com",                 # Prod
]


async def get_antigravity_endpoint_fallbacks() -> list:
    """
    Get list of Antigravity API endpoints to try in order.

    If the user has configured a custom ANTIGRAVITY_API_URL, it will be
    placed first in the list (deduped if it matches a default endpoint).
    """
    configured = await get_antigravity_api_url()
    endpoints = [configured]
    for ep in ANTIGRAVITY_ENDPOINT_FALLBACKS:
        if ep != configured:
            endpoints.append(ep)
    return endpoints
