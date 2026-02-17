"""
Antigravity API Client - Handles communication with Google's Antigravity API
处理与 Google Antigravity API 的通信
"""

import asyncio
import json
import time
import uuid
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Response
from config import (
    get_antigravity_api_url,
    get_antigravity_endpoint_fallbacks,
    get_antigravity_stream2nostream,
    get_auto_ban_error_codes,
    get_entitlement_403_model_cooldown_seconds,
    get_model_not_found_404_model_cooldown_seconds,
    get_long_quota_cooldown_rotate_threshold_seconds,
    get_retry_rotate_delay_ms,
    get_pool_wait_enabled,
    get_pool_wait_max_seconds,
    get_pool_wait_poll_seconds,
)
from log import log

from src.credential_manager import credential_manager
from src.httpx_client import stream_post_async, post_async
from src.models import Model, model_to_dict
from src.utils import ANTIGRAVITY_USER_AGENT

# 导入共同的基础功能
from src.api.utils import (
    handle_error_with_retry,
    check_should_auto_ban,
    handle_auto_ban,
    get_retry_config,
    record_api_call_success,
    record_api_call_error,
    parse_and_log_cooldown,
    collect_streaming_response,
    extract_google_error_message,
    is_entitlement_403_error,
    is_project_license_403_error,
)

from src.api.quota_refresh import fetch_realtime_quota_reset_timestamp

from src.google_oauth_api import Credentials, fetch_project_id

from src.audit_log import set_audit_context, increment_audit_attempt


def _build_no_credentials_response(snapshot: Dict[str, Any]) -> Response:
    status = 429 if snapshot.get("enabled") and snapshot.get("available") == 0 else 503
    headers_out: Dict[str, str] = {}
    earliest = snapshot.get("earliest_model_cooldown_until")
    if status == 429 and earliest:
        try:
            wait_s = max(1, int(math.ceil(float(earliest) - time.time())))
            headers_out["Retry-After"] = str(wait_s)
            headers_out["retry-after-ms"] = str(wait_s * 1000)
        except Exception:
            pass
    return Response(
        content=json.dumps({"error": "当前无可用凭证", "details": snapshot}),
        status_code=status,
        headers=headers_out or None,
        media_type="application/json",
    )

# 导入重试策略模块
from src.api.retry_strategy import (
    determine_retry_strategy,
    apply_retry_delay,
    should_rotate_account,
    is_retryable_error,
    RetryStrategy,
)

# ==================== 全局凭证管理器 ====================

# 使用全局单例 credential_manager，自动初始化


# ==================== 多端点降级 (Multi-endpoint fallback) ====================

def _should_try_next_endpoint(status_code: int) -> bool:
    """Whether to try the next endpoint on this status code.
    Matches Antigravity-Manager's should_try_next_endpoint logic."""
    return status_code in (429, 408, 404) or 500 <= status_code < 600


def _is_model_not_found_404(status_code: int, error_text: str) -> bool:
    """Detect model-not-found 404s that should fail over to another credential.

    We only classify explicit upstream model lookup failures, not generic 404s.
    """
    if status_code != 404:
        return False

    lower = (error_text or "").lower()
    if not lower:
        return False

    return (
        "requested entity was not found" in lower
        or '"status": "not_found"' in lower
        or '"status":"not_found"' in lower
    )


async def _stream_with_endpoint_fallback(
    endpoints: list,
    method_path: str,
    body: dict,
    native: bool,
    headers: dict,
):
    """Try each endpoint sequentially, yield chunks from the first that succeeds.

    If the first chunk is an error Response with a fallback-eligible status code,
    try the next endpoint. Otherwise yield all chunks from that endpoint.
    """
    last_error_chunk = None

    for idx, endpoint in enumerate(endpoints):
        url = f"{endpoint}/{method_path}"
        has_next = idx + 1 < len(endpoints)
        fell_through = False

        async for chunk in stream_post_async(url=url, body=body, native=native, headers=headers):
            if isinstance(chunk, Response) and has_next and _should_try_next_endpoint(chunk.status_code):
                log.warning(
                    f"[ENDPOINT FALLBACK] {endpoint} returned {chunk.status_code}, "
                    f"trying next endpoint ({idx + 2}/{len(endpoints)})"
                )
                last_error_chunk = chunk
                fell_through = True
                break
            else:
                yield chunk

        if not fell_through:
            return

    # All endpoints exhausted — yield last error so caller's retry loop can handle it
    if last_error_chunk:
        yield last_error_chunk


async def _post_with_endpoint_fallback(
    endpoints: list,
    method_path: str,
    json_body: dict,
    headers: dict,
    timeout: float = 300.0,
):
    """Try each endpoint for a non-streaming POST, return first success or last error."""
    last_response = None

    for idx, endpoint in enumerate(endpoints):
        url = f"{endpoint}/{method_path}"
        has_next = idx + 1 < len(endpoints)

        response = await post_async(url=url, json=json_body, headers=headers, timeout=timeout)

        if response.status_code == 200:
            if idx > 0:
                log.info(
                    f"[ENDPOINT FALLBACK] Succeeded on endpoint {idx + 1}/{len(endpoints)}: {endpoint}"
                )
            return response

        if has_next and _should_try_next_endpoint(response.status_code):
            log.warning(
                f"[ENDPOINT FALLBACK] {endpoint} returned {response.status_code}, "
                f"trying next endpoint ({idx + 2}/{len(endpoints)})"
            )
            last_response = response
            continue

        return response  # Non-retryable at endpoint level

    return last_response


# ==================== 辅助函数 ====================


def _log_payload_debug(label: str, payload: dict, status_code: int = 0, credential: str = ""):
    """Log request payload summary on errors for diagnosis."""
    import json as _json
    try:
        request = payload.get("request", {})
        contents = request.get("contents", [])
        debug = {
            "status": status_code,
            "credential": credential[:30] if credential else "",
            "model": payload.get("model"),
            "generationConfig": request.get("generationConfig"),
            "contents_count": len(contents),
            "tools_count": len(request.get("tools", []) or []),
            "has_systemInstruction": "systemInstruction" in request,
        }
        if contents:
            last = contents[-1]
            debug["last_content_role"] = last.get("role")
            parts = last.get("parts", [])
            debug["last_content_parts_count"] = len(parts)
            if parts:
                debug["last_part_keys"] = [
                    list(p.keys()) if isinstance(p, dict) else type(p).__name__
                    for p in parts[:3]
                ]
        log.error(f"[{label}] REQUEST DEBUG: {_json.dumps(debug, default=str)}")
    except Exception as e:
        log.error(f"[{label}] REQUEST DEBUG failed: {e}")

def build_antigravity_headers(access_token: str, model_name: str = "") -> Dict[str, str]:
    """
    构建 Antigravity API 请求头

    Args:
        access_token: 访问令牌
        model_name: 模型名称，用于判断 request_type

    Returns:
        请求头字典
    """
    headers = {
        'User-Agent': ANTIGRAVITY_USER_AGENT,
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'Accept-Encoding': 'gzip',
        'requestId': f"req-{uuid.uuid4()}"
    }

    # 根据模型名称判断 request_type
    if model_name:
        # 先判断是否是图片模型
        if "image" in model_name.lower():
            request_type = "image_gen"
            headers['requestType'] = request_type
        else:
            request_type = "agent"
            headers['requestType'] = request_type

    return headers


# ==================== 新的流式和非流式请求函数 ====================

async def stream_request(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
):
    """
    流式请求函数

    Args:
        body: 请求体
        native: 是否返回原生bytes流，False则返回str流
        headers: 额外的请求头

    Yields:
        Response对象（错误时）或 bytes流/str流（成功时）
    """
    model_name = body.get("model", "")

    # Set up audit context for this request
    set_audit_context(mode="antigravity", model=model_name, streaming=True, request_payload=body)

    def _is_safety_settings_error(text: str) -> bool:
        t = (text or "").lower()
        return ("safety_settings" in t) and ("element predicate failed" in t)

    def _next_fallback_model(current: str) -> Optional[str]:
        # Defensive fallback chain for flash-tier models.
        # We see some models reject certain safety_settings; fall back progressively.
        chain = [
            "gemini-2.5-flash",
            "gemini-3-flash-low",
            "gemini-3-flash-high",
        ]

        # Normalize a couple of equivalent/legacy names into the chain.
        if current == "gemini-2.5-flash-lite":
            current = "gemini-2.5-flash"

        try:
            idx = chain.index(current)
        except ValueError:
            return None
        if idx + 1 >= len(chain):
            return None
        return chain[idx + 1]

    # Track tried credentials for this request to avoid repeatedly hitting
    # the same rate-limited/denied account when retrying.
    tried_files: set[str] = set()

    # 1. 获取有效凭证
    cred_result = await credential_manager.get_valid_credential(
        mode="antigravity", model_key=model_name
    )

    if not cred_result and await get_pool_wait_enabled():
        try:
            cred_result = await credential_manager.wait_for_valid_credential(
                mode="antigravity",
                model_key=model_name,
                exclude_filenames=None,
                max_wait_seconds=await get_pool_wait_max_seconds(),
                poll_seconds=await get_pool_wait_poll_seconds(),
            )
        except Exception as e:
            log.warning(f"[ANTIGRAVITY STREAM] Pool wait failed: {e}")

    if not cred_result:
        snapshot = await credential_manager.get_model_availability_snapshot(
            mode="antigravity", model_key=model_name, exclude_filenames=None
        )
        log.error(f"[ANTIGRAVITY STREAM] 当前无可用凭证: {snapshot}")
        yield _build_no_credentials_response(snapshot)
        return

    current_file, credential_data = cred_result
    tried_files.add(str(current_file))
    log.debug(f"[ANTIGRAVITY STREAM] Selected credential={current_file} for model_key={model_name}")
    access_token = credential_data.get("access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id", "")

    if not access_token:
        log.error(f"[ANTIGRAVITY STREAM] No access token in credential: {current_file}")
        yield Response(
            content=json.dumps({"error": "凭证中没有访问令牌"}),
            status_code=500,
            media_type="application/json"
        )
        return

    # 2. 构建URL和请求头
    endpoint_fallbacks = await get_antigravity_endpoint_fallbacks()

    auth_headers = build_antigravity_headers(access_token, model_name)

    # 合并自定义headers
    if headers:
        auth_headers.update(headers)

    # 构建包含project的payload
    final_payload = {
        "model": body.get("model"),
        "project": project_id,
        "request": body.get("request", {}),
    }

    # -------------------------------------------------------------------------
    # Adaptive thinking emulation via effort → thinkingBudget mapping
    # -------------------------------------------------------------------------
    # Antigravity rejects effortLevel with 400, but we can emulate effort
    # by scaling thinkingBudget — the only lever available for Claude.
    gen_config = final_payload.get("request", {}).get("generationConfig")
    if isinstance(gen_config, dict):
        effort = gen_config.pop("effortLevel", None)
        if effort:
            effort_budget_map = {
                "LOW": 4096,       # Fast: minimal thinking for simple tasks
                "MEDIUM": 16384,   # Balanced: standard reasoning depth
                "HIGH": 32000,     # Deep: matches Claude Code's default (31999 rounded)
            }
            target_budget = effort_budget_map.get(effort, 32000)
            thinking_config = gen_config.get("thinkingConfig")
            if isinstance(thinking_config, dict):
                # Don't downgrade if upstream converter already set a higher budget
                # (e.g., adaptive "max" effort → 48000)
                current = thinking_config.get("thinking_budget") or thinking_config.get("thinkingBudget") or 0
                if current > target_budget:
                    log.info(f"[ADAPTIVE-EMULATION] effort={effort} → keeping existing budget {current} (> {target_budget})")
                else:
                    # gemini_fix.py converts to snake_case for Claude models
                    if "thinking_budget" in thinking_config:
                        thinking_config["thinking_budget"] = target_budget
                    else:
                        thinking_config["thinkingBudget"] = target_budget
                    log.info(f"[ADAPTIVE-EMULATION] effort={effort} → thinkingBudget={target_budget}")

    # 3. 调用stream_post_async进行请求
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_response = None  # 记录最后一次的错误响应
    last_error_body = ""  # 记录最后一次的错误内容 (用于智能重试策略)
    next_cred_task = None  # 预热的下一个凭证任务
    project_fix_attempted: set[str] = set()

    # 内部函数：快速更新凭证(只更新token和project_id,避免重建整个请求)
    async def refresh_credential_fast():
        nonlocal current_file, credential_data, access_token, auth_headers, project_id, final_payload
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity", model_key=model_name, exclude_filenames=list(tried_files)
        )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        tried_files.add(str(current_file))
        log.debug(f"[ANTIGRAVITY STREAM] Rotated credential={current_file} for model_key={model_name}")
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token:
            return None
        # 只更新token和project_id,不重建整个headers和payload
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    for attempt in range(max_retries + 1):
        success_recorded = False  # 标记是否已记录成功
        need_retry = False  # 标记是否需要重试
        force_rotate_credential = False
        last_status_code_for_retry: Optional[int] = None

        # Track attempt for audit
        if attempt > 0:
            increment_audit_attempt()

        try:
            async for chunk in _stream_with_endpoint_fallback(
                endpoints=endpoint_fallbacks,
                method_path="v1internal:streamGenerateContent?alt=sse",
                body=final_payload,
                native=native,
                headers=auth_headers
            ):
                # 判断是否是Response对象
                if isinstance(chunk, Response):
                    status_code = chunk.status_code
                    last_status_code_for_retry = status_code
                    last_error_response = chunk  # 记录最后一次错误

                    # 缓存错误解析结果,避免重复decode
                    error_body = None
                    try:
                        error_body = chunk.body.decode('utf-8') if isinstance(chunk.body, bytes) else str(chunk.body)
                    except Exception:
                        error_body = ""

                    # 保存错误内容用于智能重试策略
                    last_error_body = error_body or ""

                    # Credential-specific model not found (404): model-level cooldown + rotate account.
                    if _is_model_not_found_404(status_code, error_body or ""):
                        cooldown_secs = await get_model_not_found_404_model_cooldown_seconds()
                        cooldown_until = None
                        if cooldown_secs > 0:
                            cooldown_until = time.time() + float(cooldown_secs)

                        log.warning(
                            f"[ANTIGRAVITY STREAM] Model not found for credential; "
                            f"applying model cooldown and rotating. status=404, "
                            f"credential={current_file}, model={model_name}, cooldown_secs={cooldown_secs}"
                        )

                        await record_api_call_error(
                            credential_manager,
                            current_file,
                            status_code,
                            cooldown_until,
                            mode="antigravity",
                            model_key=model_name,
                            error_text=error_body or "",
                        )

                        if attempt < max_retries:
                            force_rotate_credential = True
                            need_retry = True
                            if next_cred_task is None:
                                next_cred_task = asyncio.create_task(
                                    credential_manager.get_valid_credential(
                                        mode="antigravity",
                                        model_key=model_name,
                                        exclude_filenames=list(tried_files),
                                    )
                                )
                            delay_ms = await get_retry_rotate_delay_ms()
                            await asyncio.sleep(delay_ms / 1000.0)
                            break

                        log.error(f"[ANTIGRAVITY STREAM] 达到最大重试次数 ({max_retries})，返回原始错误")
                        _log_payload_debug("ANTIGRAVITY STREAM MAX-RETRY", final_payload, status_code, current_file)
                        yield chunk
                        return

                    # Defensive model fallback (mainly for Haiku→Flash routing)
                    # If upstream rejects our request due to safety_settings validation,
                    # retry once with a more compatible flash model.
                    if status_code == 400 and _is_safety_settings_error(error_body or ""):
                        next_model = _next_fallback_model(str(model_name))
                        if next_model:
                            log.warning(
                                f"[ANTIGRAVITY STREAM] safety_settings rejected for model={model_name}; "
                                f"falling back to model={next_model}"
                            )

                            # Switch model and re-select a credential for the new model_key.
                            model_name = next_model
                            final_payload["model"] = model_name

                            # Reset try history so the new model can use the full pool.
                            tried_files.clear()
                            project_fix_attempted.clear()
                            next_cred_task = None

                            cred_result2 = await credential_manager.get_valid_credential(
                                mode="antigravity", model_key=model_name
                            )
                            if not cred_result2 and await get_pool_wait_enabled():
                                try:
                                    cred_result2 = await credential_manager.wait_for_valid_credential(
                                        mode="antigravity",
                                        model_key=model_name,
                                        exclude_filenames=None,
                                        max_wait_seconds=await get_pool_wait_max_seconds(),
                                        poll_seconds=await get_pool_wait_poll_seconds(),
                                    )
                                except Exception as e:
                                    log.warning(f"[ANTIGRAVITY STREAM] Pool wait failed (fallback): {e}")

                            if not cred_result2:
                                snapshot = await credential_manager.get_model_availability_snapshot(
                                    mode="antigravity", model_key=model_name, exclude_filenames=None
                                )
                                log.error(f"[ANTIGRAVITY STREAM] 当前无可用凭证 (fallback): {snapshot}")
                                yield _build_no_credentials_response(snapshot)
                                return

                            current_file, credential_data = cred_result2
                            tried_files.add(str(current_file))
                            log.debug(
                                f"[ANTIGRAVITY STREAM] Selected credential={current_file} for model_key={model_name}"
                            )

                            access_token = credential_data.get("access_token") or credential_data.get("token")
                            project_id = credential_data.get("project_id", "")
                            if not access_token:
                                log.error(
                                    f"[ANTIGRAVITY STREAM] No access token in credential (fallback): {current_file}"
                                )
                                yield Response(
                                    content=json.dumps({"error": "凭证中没有访问令牌"}),
                                    status_code=500,
                                    media_type="application/json",
                                )
                                return

                            auth_headers = build_antigravity_headers(access_token, model_name)
                            if headers:
                                auth_headers.update(headers)
                            final_payload["project"] = project_id

                            need_retry = True
                            # Use a non-rotating status so the retry loop keeps the newly selected credential.
                            last_status_code_for_retry = 503
                            break

                    # 使用新的重试策略判断是否应该重试
                    strategy, base_ms, max_ms, reason = determine_retry_strategy(
                        status_code, error_body or "",
                        credential_id=str(current_file),
                        model=str(model_name),
                    )

                    retry_after_header = None
                    try:
                        retry_after_header = chunk.headers.get("Retry-After") if getattr(chunk, "headers", None) else None
                    except Exception:
                        retry_after_header = None

                    # Special handling: entitlement/project permission errors should not spam the pool.
                    extra_cooldown_until = None
                    if status_code == 403 and error_body:
                        if is_project_license_403_error(error_body) and current_file not in project_fix_attempted:
                            project_fix_attempted.add(str(current_file))
                            try:
                                # Try to refresh token + re-fetch project_id to recover from #3501-style errors.
                                creds = Credentials.from_dict(credential_data)
                                refreshed = await creds.refresh_if_needed()
                                if refreshed:
                                    updated = creds.to_dict()
                                    await credential_manager.add_antigravity_credential(current_file, updated)
                                    credential_data = updated
                                    access_token = updated.get("access_token") or updated.get("token")
                                    auth_headers["Authorization"] = f"Bearer {access_token}"

                                if not access_token:
                                    raise ValueError("missing access_token after refresh")

                                api_base_url = await get_antigravity_api_url()
                                new_project_id = await fetch_project_id(
                                    access_token=str(access_token),
                                    user_agent=ANTIGRAVITY_USER_AGENT,
                                    api_base_url=api_base_url,
                                )
                                if new_project_id and new_project_id != project_id:
                                    project_id = new_project_id
                                    final_payload["project"] = project_id
                                    credential_data["project_id"] = project_id
                                    await credential_manager.add_antigravity_credential(current_file, credential_data)
                                    await credential_manager.update_credential_state(
                                        current_file,
                                        {"disabled": False, "disabled_reason": None, "error_codes": []},
                                        mode="antigravity",
                                    )
                                    log.info(
                                        f"[ANTIGRAVITY STREAM] Recovered by updating project_id for {current_file}"
                                    )

                                    # Retry immediately on the same credential.
                                    need_retry = True
                                    break
                            except Exception as e:
                                log.warning(f"[ANTIGRAVITY STREAM] Project re-resolve failed: {e}")

                        if is_entitlement_403_error(error_body):
                            cooldown_secs = await get_entitlement_403_model_cooldown_seconds()
                            extra_cooldown_until = time.time() + cooldown_secs

                    # For long quota reset delays, rotate immediately instead of sleeping.
                    immediate_rotate = False
                    parsed_until: Optional[float] = None
                    if status_code == 429 and error_body:
                        try:
                            parsed_until = await parse_and_log_cooldown(
                                error_body, mode="antigravity", retry_after_header=retry_after_header
                            )
                        except Exception:
                            parsed_until = None
                        if parsed_until is not None:
                            threshold = await get_long_quota_cooldown_rotate_threshold_seconds()
                            if (parsed_until - time.time()) > float(threshold):
                                immediate_rotate = True

                    if immediate_rotate:
                        # Record with precise model cooldown then rotate without waiting.
                        await record_api_call_error(
                            credential_manager,
                            current_file,
                            status_code,
                            parsed_until,
                            mode="antigravity",
                            model_key=model_name,
                            error_text=error_body or "",
                        )
                        need_retry = attempt < max_retries
                        if need_retry:
                            delay_ms = await get_retry_rotate_delay_ms()
                            await asyncio.sleep(delay_ms / 1000.0)
                            break
                        log.error(f"[ANTIGRAVITY STREAM] 达到最大重试次数 ({max_retries})，返回原始错误")
                        _log_payload_debug("ANTIGRAVITY STREAM MAX-RETRY", final_payload, status_code, current_file)
                        yield chunk
                        return

                    if strategy != RetryStrategy.NO_RETRY:
                        # 可重试的错误 (429, 503, 529, 500, 401, 403)
                        log.warning(
                            f"[ANTIGRAVITY STREAM] 流式请求失败 (status={status_code}), "
                            f"策略={strategy.value}, 凭证: {current_file}, "
                            f"响应: {error_body[:500] if error_body else '无'}"
                        )

                        # 并行预热下一个凭证 (仅在需要轮换账号时)
                        if should_rotate_account(status_code, error_body or ""):
                            if next_cred_task is None and attempt < max_retries:
                                next_cred_task = asyncio.create_task(
                                    credential_manager.get_valid_credential(
                                        mode="antigravity",
                                        model_key=model_name,
                                        exclude_filenames=list(tried_files),
                                    )
                                )

                        # 记录错误
                        cooldown_until = extra_cooldown_until
                        if cooldown_until is None and status_code == 429 and error_body:
                            try:
                                cooldown_until = await parse_and_log_cooldown(
                                    error_body, mode="antigravity", retry_after_header=retry_after_header
                                )
                            except Exception:
                                pass

                        # Realtime quota refresh fallback (Antigravity-Manager style)
                        if cooldown_until is None and status_code == 429:
                            try:
                                from config import (
                                    get_realtime_quota_refresh_enabled,
                                    get_realtime_quota_refresh_timeout_seconds,
                                    get_realtime_quota_refresh_cache_ttl_seconds,
                                    get_realtime_quota_refresh_fallback_to_earliest_reset,
                                )

                                if await get_realtime_quota_refresh_enabled():
                                    api_base_url = await get_antigravity_api_url()
                                    cooldown_until = await fetch_realtime_quota_reset_timestamp(
                                        api_base_url=api_base_url,
                                        headers=auth_headers,
                                        model_name=str(model_name),
                                        cache_key=str(current_file),
                                        cache_ttl_seconds=int(await get_realtime_quota_refresh_cache_ttl_seconds()),
                                        timeout_seconds=float(await get_realtime_quota_refresh_timeout_seconds()),
                                        fallback_to_earliest=bool(
                                            await get_realtime_quota_refresh_fallback_to_earliest_reset()
                                        ),
                                    )
                            except Exception as e:
                                log.debug(f"[ANTIGRAVITY STREAM] Realtime quota refresh failed: {e}")

                        await record_api_call_error(
                            credential_manager, current_file, status_code,
                            cooldown_until, mode="antigravity", model_key=model_name,
                            error_text=error_body or "",
                        )
                        # Auto-ban: disable credential on DISABLE_ERROR_CODES
                        if await check_should_auto_ban(status_code):
                            await handle_auto_ban(
                                credential_manager, status_code, current_file,
                                mode="antigravity", error_text=error_body or "",
                            )

                        # 应用重试延迟
                        if attempt < max_retries:
                            should_continue = await apply_retry_delay(
                                strategy, base_ms, max_ms, attempt,
                                trace_id=f"ANTIGRAVITY-STREAM-{current_file[:20]}"
                            )
                            if should_continue:
                                need_retry = True
                                break  # 跳出内层循环，准备重试
                        
                        # 达到最大重试次数
                        log.error(f"[ANTIGRAVITY STREAM] 达到最大重试次数 ({max_retries})，返回原始错误")
                        _log_payload_debug("ANTIGRAVITY STREAM MAX-RETRY", final_payload, status_code, current_file)
                        yield chunk
                        return
                    else:
                        # 不可重试的错误 (400等)
                        log.error(
                            f"[ANTIGRAVITY STREAM] 流式请求失败，非重试错误码 (status={status_code}), "
                            f"凭证: {current_file}, 响应: {error_body[:500] if error_body else '无'}"
                        )
                        _log_payload_debug("ANTIGRAVITY STREAM", final_payload, status_code, current_file)
                        await record_api_call_error(
                            credential_manager, current_file, status_code,
                            None, mode="antigravity", model_key=model_name,
                            error_text=error_body or "",
                        )
                        yield chunk
                        return
                else:
                    # 不是Response，说明是真流，直接yield返回
                    # 只在第一个chunk时记录成功
                    if not success_recorded:
                        await record_api_call_success(
                            credential_manager, current_file, mode="antigravity", model_key=model_name
                        )
                        success_recorded = True
                        log.debug(f"[ANTIGRAVITY STREAM] 开始接收流式响应，模型: {model_name}")

                    # 记录原始chunk内容（用于调试）
                    # Check for thoughtSignature in raw response
                    chunk_str = chunk.decode('utf-8', errors='ignore') if isinstance(chunk, bytes) else str(chunk)
                    if 'functionCall' in chunk_str:
                        has_sig = 'thoughtSignature' in chunk_str
                        log.info(f"[UPSTREAM_RAW] functionCall detected, has_thoughtSignature={has_sig}")
                        # Log full chunk for debugging signature issue
                        log.info(f"[UPSTREAM_RAW] FULL functionCall chunk: {chunk_str[:1000]}")
                        if has_sig:
                            # Try to extract and log the signature presence
                            import re
                            sig_match = re.search(r'"thoughtSignature"\s*:\s*"([^"]{0,50})', chunk_str)
                            if sig_match:
                                log.info(f"[UPSTREAM_RAW] thoughtSignature preview: {sig_match.group(1)}...")
                        else:
                            log.warning(f"[UPSTREAM_RAW] NO thoughtSignature in functionCall response!")
                    if isinstance(chunk, bytes):
                        log.debug(f"[ANTIGRAVITY STREAM RAW] chunk(bytes): {chunk[:500] if len(chunk) > 500 else chunk}")
                    else:
                        log.debug(f"[ANTIGRAVITY STREAM RAW] chunk(str): {chunk[:500] if len(chunk) > 500 else chunk}")

                    yield chunk

            # 流式请求完成，检查结果
            if success_recorded:
                log.debug(f"[ANTIGRAVITY STREAM] 流式响应完成，模型: {model_name}")
                return
            elif not need_retry:
                # 没有收到任何数据（空回复），需要重试
                log.warning(f"[ANTIGRAVITY STREAM] 收到空回复，无任何内容，凭证: {current_file}")
                last_status_code_for_retry = 200
                await record_api_call_error(
                    credential_manager, current_file, 200,
                    None, mode="antigravity", model_key=model_name,
                    error_text="empty_response",
                )
                
                if attempt < max_retries:
                    need_retry = True
                else:
                    log.error(f"[ANTIGRAVITY STREAM] 空回复达到最大重试次数")
                    _log_payload_debug("ANTIGRAVITY STREAM EMPTY-RESP", final_payload, 200, current_file)
                    yield Response(
                        content=json.dumps({"error": "服务返回空回复"}),
                        status_code=500,
                        media_type="application/json"
                    )
                    return
            
            # 统一处理重试
            if need_retry:
                log.info(f"[ANTIGRAVITY STREAM] 重试请求 (attempt {attempt + 2}/{max_retries + 1})...")

                # For non-rotating errors (e.g. empty response), keep the same credential.
                # For capacity errors (503/529), rotation is handled by should_rotate_account.
                if (
                    not force_rotate_credential
                    and
                    last_status_code_for_retry is not None
                    and not should_rotate_account(int(last_status_code_for_retry), last_error_body)
                ):
                    log.info(
                        f"[ANTIGRAVITY STREAM] Keeping same credential for status={last_status_code_for_retry}"
                    )
                    continue

                # 使用预热的凭证任务,避免等待
                if next_cred_task is not None:
                    try:
                        cred_result = await next_cred_task
                        next_cred_task = None  # 重置任务

                        if cred_result:
                            current_file, credential_data = cred_result
                            tried_files.add(str(current_file))
                            access_token = credential_data.get("access_token") or credential_data.get("token")
                            project_id = credential_data.get("project_id", "")
                            if access_token and project_id:
                                auth_headers["Authorization"] = f"Bearer {access_token}"
                                final_payload["project"] = project_id
                                continue  # 重试
                    except Exception as e:
                        log.warning(f"[ANTIGRAVITY STREAM] 预热凭证任务失败: {e}")
                        next_cred_task = None

                # 如果预热的凭证不可用,则同步获取

                if not await refresh_credential_fast():
                    log.error("[ANTIGRAVITY STREAM] 重试时无可用凭证或令牌")
                    # As a last resort, wait briefly for the pool to recover.
                    if await get_pool_wait_enabled():
                        try:
                            waited = await credential_manager.wait_for_valid_credential(
                                mode="antigravity",
                                model_key=model_name,
                                exclude_filenames=None,
                                max_wait_seconds=await get_pool_wait_max_seconds(),
                                poll_seconds=await get_pool_wait_poll_seconds(),
                            )
                            if waited:
                                current_file, credential_data = waited
                                tried_files.add(str(current_file))
                                access_token = credential_data.get("access_token") or credential_data.get("token")
                                project_id = credential_data.get("project_id", "")
                                if access_token and project_id:
                                    auth_headers["Authorization"] = f"Bearer {access_token}"
                                    final_payload["project"] = project_id
                                    continue
                        except Exception as e:
                            log.warning(f"[ANTIGRAVITY STREAM] Pool wait during retry failed: {e}")

                    snapshot = await credential_manager.get_model_availability_snapshot(
                        mode="antigravity", model_key=model_name, exclude_filenames=None
                    )
                    yield _build_no_credentials_response(snapshot)
                    return
                continue  # 重试

        except Exception as e:
            log.error(f"[ANTIGRAVITY STREAM] 流式请求异常: {e}, 凭证: {current_file}")
            if attempt < max_retries:
                log.info(f"[ANTIGRAVITY STREAM] 异常后重试 (attempt {attempt + 2}/{max_retries + 1})...")
                delay_ms = await get_retry_rotate_delay_ms()
                await asyncio.sleep(delay_ms / 1000.0)
                continue
            else:
                # 所有重试都失败，返回最后一次的错误（如果有）
                log.error(f"[ANTIGRAVITY STREAM] 所有重试均失败，最后异常: {e}")
                if last_error_response:
                    yield last_error_response
                else:
                    yield Response(
                        content=json.dumps({"error": f"流式请求异常: {str(e)}"}),
                        status_code=500,
                        media_type="application/json"
                    )
                return


async def non_stream_request(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
) -> Response:
    """
    非流式请求函数

    Args:
        body: 请求体
        headers: 额外的请求头

    Returns:
        Response对象
    """
    # 检查是否启用流式收集模式
    if await get_antigravity_stream2nostream():
        log.debug("[ANTIGRAVITY] 使用流式收集模式实现非流式请求")

        # 调用stream_request获取流
        stream = stream_request(body=body, native=False, headers=headers)

        # 收集流式响应
        # stream_request是一个异步生成器，可能yield Response（错误）或流数据
        # collect_streaming_response会自动处理这两种情况
        return await collect_streaming_response(stream)

    # 否则使用传统非流式模式
    log.debug("[ANTIGRAVITY] 使用传统非流式模式")

    model_name = body.get("model", "")

    # Set up audit context for this request (non-streaming path)
    set_audit_context(mode="antigravity", model=model_name, streaming=False, request_payload=body)

    # Track tried credentials for this request to avoid repeatedly hitting
    # the same rate-limited/denied account when retrying.
    tried_files: set[str] = set()

    # 1. 获取有效凭证
    cred_result = await credential_manager.get_valid_credential(
        mode="antigravity", model_key=model_name
    )

    if not cred_result and await get_pool_wait_enabled():
        try:
            cred_result = await credential_manager.wait_for_valid_credential(
                mode="antigravity",
                model_key=model_name,
                exclude_filenames=None,
                max_wait_seconds=await get_pool_wait_max_seconds(),
                poll_seconds=await get_pool_wait_poll_seconds(),
            )
        except Exception as e:
            log.warning(f"[ANTIGRAVITY] Pool wait failed: {e}")

    if not cred_result:
        snapshot = await credential_manager.get_model_availability_snapshot(
            mode="antigravity", model_key=model_name, exclude_filenames=None
        )
        log.error(f"[ANTIGRAVITY] 当前无可用凭证: {snapshot}")
        return _build_no_credentials_response(snapshot)

    current_file, credential_data = cred_result
    tried_files.add(str(current_file))
    log.debug(f"[ANTIGRAVITY] Selected credential={current_file} for model_key={model_name}")
    access_token = credential_data.get("access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id", "")

    if not access_token:
        log.error(f"[ANTIGRAVITY] No access token in credential: {current_file}")
        return Response(
            content=json.dumps({"error": "凭证中没有访问令牌"}),
            status_code=500,
            media_type="application/json"
        )

    # 2. 构建URL和请求头
    endpoint_fallbacks = await get_antigravity_endpoint_fallbacks()

    auth_headers = build_antigravity_headers(access_token, model_name)

    # 合并自定义headers
    if headers:
        auth_headers.update(headers)

    # 构建包含project的payload
    final_payload = {
        "model": body.get("model"),
        "project": project_id,
        "request": body.get("request", {}),
    }

    # -------------------------------------------------------------------------
    # Adaptive thinking emulation via effort → thinkingBudget mapping
    # -------------------------------------------------------------------------
    gen_config = final_payload.get("request", {}).get("generationConfig")
    if isinstance(gen_config, dict):
        effort = gen_config.pop("effortLevel", None)
        if effort:
            effort_budget_map = {
                "LOW": 4096,
                "MEDIUM": 16384,
                "HIGH": 32000,
            }
            target_budget = effort_budget_map.get(effort, 32000)
            thinking_config = gen_config.get("thinkingConfig")
            if isinstance(thinking_config, dict):
                # Don't downgrade if upstream converter already set a higher budget
                current = thinking_config.get("thinking_budget") or thinking_config.get("thinkingBudget") or 0
                if current > target_budget:
                    log.info(f"[ADAPTIVE-EMULATION] effort={effort} → keeping existing budget {current} (> {target_budget})")
                else:
                    if "thinking_budget" in thinking_config:
                        thinking_config["thinking_budget"] = target_budget
                    else:
                        thinking_config["thinkingBudget"] = target_budget
                    log.info(f"[ADAPTIVE-EMULATION] effort={effort} → thinkingBudget={target_budget}")

    # 3. 调用post_async进行请求
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_response = None  # 记录最后一次的错误响应
    next_cred_task = None  # 预热的下一个凭证任务
    project_fix_attempted: set[str] = set()

    # 内部函数：快速更新凭证(只更新token和project_id,避免重建整个请求)
    async def refresh_credential_fast():
        nonlocal current_file, credential_data, access_token, auth_headers, project_id, final_payload
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity", model_key=model_name, exclude_filenames=list(tried_files)
        )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        tried_files.add(str(current_file))
        log.debug(f"[ANTIGRAVITY] Rotated credential={current_file} for model_key={model_name}")
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token:
            return None
        # 只更新token和project_id,不重建整个headers和payload
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    for attempt in range(max_retries + 1):
        need_retry = False  # 标记是否需要重试
        force_rotate_credential = False

        # Track attempt for audit
        if attempt > 0:
            increment_audit_attempt()

        try:
            response = await _post_with_endpoint_fallback(
                endpoints=endpoint_fallbacks,
                method_path="v1internal:generateContent",
                json_body=final_payload,
                headers=auth_headers,
                timeout=300.0
            )

            status_code = response.status_code

            # 成功
            if status_code == 200:
                # 检查是否为空回复
                if not response.content or len(response.content) == 0:
                    log.warning(f"[ANTIGRAVITY] 收到200响应但内容为空，凭证: {current_file}")
                    
                    # 记录错误
                    await record_api_call_error(
                        credential_manager, current_file, 200,
                        None, mode="antigravity", model_key=model_name,
                        error_text="empty_response",
                    )
                    
                    if attempt < max_retries:
                        need_retry = True
                    else:
                        log.error(f"[ANTIGRAVITY] 空回复达到最大重试次数")
                        _log_payload_debug("ANTIGRAVITY EMPTY-RESP", final_payload, 200, current_file)
                        return Response(
                            content=json.dumps({"error": "服务返回空回复"}),
                            status_code=500,
                            media_type="application/json"
                        )
                else:
                    # 正常响应
                    await record_api_call_success(
                        credential_manager, current_file, mode="antigravity", model_key=model_name
                    )
                    return Response(
                        content=response.content,
                        status_code=200,
                        headers=dict(response.headers)
                    )

            # 失败 - 记录最后一次错误
            if status_code != 200:
                last_error_response = Response(
                    content=response.content,
                    status_code=status_code,
                    headers=dict(response.headers)
                )

                # 判断是否需要重试
                # 缓存错误文本,避免重复解析
                error_text = ""
                try:
                    error_text = response.text
                except Exception:
                    pass

                retry_after_header = None
                try:
                    retry_after_header = response.headers.get("Retry-After")
                except Exception:
                    retry_after_header = None

                # 使用新的重试策略判断是否应该重试
                strategy, base_ms, max_ms, reason = determine_retry_strategy(
                    status_code, error_text or "",
                    credential_id=str(current_file),
                    model=str(model_name),
                )

                # Credential-specific model not found (404): model-level cooldown + rotate account.
                if _is_model_not_found_404(status_code, error_text or ""):
                    cooldown_secs = await get_model_not_found_404_model_cooldown_seconds()
                    cooldown_until = None
                    if cooldown_secs > 0:
                        cooldown_until = time.time() + float(cooldown_secs)

                    log.warning(
                        f"[ANTIGRAVITY] Model not found for credential; applying model cooldown and rotating. "
                        f"status=404, credential={current_file}, model={model_name}, cooldown_secs={cooldown_secs}"
                    )

                    await record_api_call_error(
                        credential_manager,
                        current_file,
                        status_code,
                        cooldown_until,
                        mode="antigravity",
                        model_key=model_name,
                        error_text=error_text or "",
                    )

                    if attempt < max_retries:
                        force_rotate_credential = True
                        need_retry = True
                        if next_cred_task is None:
                            next_cred_task = asyncio.create_task(
                                credential_manager.get_valid_credential(
                                    mode="antigravity",
                                    model_key=model_name,
                                    exclude_filenames=list(tried_files),
                                )
                            )
                        await asyncio.sleep((await get_retry_rotate_delay_ms()) / 1000.0)
                    else:
                        log.error(f"[ANTIGRAVITY] 达到最大重试次数 ({max_retries})，返回原始错误")
                        _log_payload_debug("ANTIGRAVITY MAX-RETRY", final_payload, status_code, current_file)
                        return last_error_response

                    # Skip generic retry strategy for this classified 404.
                    if need_retry:
                        log.info(
                            f"[ANTIGRAVITY] 重试请求 (attempt {attempt + 2}/{max_retries + 1})..."
                        )

                        # 使用预热的凭证任务,避免等待
                        if next_cred_task is not None:
                            try:
                                cred_result = await next_cred_task
                                next_cred_task = None  # 重置任务

                                if cred_result:
                                    current_file, credential_data = cred_result
                                    tried_files.add(str(current_file))
                                    access_token = credential_data.get("access_token") or credential_data.get("token")
                                    project_id = credential_data.get("project_id", "")
                                    if access_token and project_id:
                                        auth_headers["Authorization"] = f"Bearer {access_token}"
                                        final_payload["project"] = project_id
                                        continue  # 重试
                            except Exception as e:
                                log.warning(f"[ANTIGRAVITY] 预热凭证任务失败: {e}")
                                next_cred_task = None

                        if not await refresh_credential_fast():
                            log.error("[ANTIGRAVITY] 重试时无可用凭证或令牌")
                            if await get_pool_wait_enabled():
                                try:
                                    waited = await credential_manager.wait_for_valid_credential(
                                        mode="antigravity",
                                        model_key=model_name,
                                        exclude_filenames=None,
                                        max_wait_seconds=await get_pool_wait_max_seconds(),
                                        poll_seconds=await get_pool_wait_poll_seconds(),
                                    )
                                    if waited:
                                        current_file, credential_data = waited
                                        tried_files.add(str(current_file))
                                        access_token = credential_data.get("access_token") or credential_data.get("token")
                                        project_id = credential_data.get("project_id", "")
                                        if access_token and project_id:
                                            auth_headers["Authorization"] = f"Bearer {access_token}"
                                            final_payload["project"] = project_id
                                            continue
                                except Exception as e:
                                    log.warning(f"[ANTIGRAVITY] Pool wait during retry failed: {e}")

                            snapshot = await credential_manager.get_model_availability_snapshot(
                                mode="antigravity", model_key=model_name, exclude_filenames=None
                            )
                            return _build_no_credentials_response(snapshot)
                        continue

                # Special handling: entitlement/project permission errors should not spam the pool.
                extra_cooldown_until = None
                if status_code == 403 and error_text:
                    if is_project_license_403_error(error_text) and current_file not in project_fix_attempted:
                        project_fix_attempted.add(str(current_file))
                        try:
                            creds = Credentials.from_dict(credential_data)
                            refreshed = await creds.refresh_if_needed()
                            if refreshed:
                                updated = creds.to_dict()
                                await credential_manager.add_antigravity_credential(current_file, updated)
                                credential_data = updated
                                access_token = updated.get("access_token") or updated.get("token")
                                auth_headers["Authorization"] = f"Bearer {access_token}"

                            if not access_token:
                                raise ValueError("missing access_token after refresh")

                            api_base_url = await get_antigravity_api_url()
                            new_project_id = await fetch_project_id(
                                access_token=str(access_token),
                                user_agent=ANTIGRAVITY_USER_AGENT,
                                api_base_url=api_base_url,
                            )
                            if new_project_id and new_project_id != project_id:
                                project_id = new_project_id
                                final_payload["project"] = project_id
                                credential_data["project_id"] = project_id
                                await credential_manager.add_antigravity_credential(current_file, credential_data)
                                await credential_manager.update_credential_state(
                                    current_file,
                                    {"disabled": False, "error_codes": []},
                                    mode="antigravity",
                                )
                                log.info(
                                    f"[ANTIGRAVITY] Recovered by updating project_id for {current_file}"
                                )

                                # Retry immediately on the same credential.
                                if attempt < max_retries:
                                    await asyncio.sleep((await get_retry_rotate_delay_ms()) / 1000.0)
                                    need_retry = True
                                    continue
                        except Exception as e:
                            log.warning(f"[ANTIGRAVITY] Project re-resolve failed: {e}")

                    if is_entitlement_403_error(error_text):
                        cooldown_secs = await get_entitlement_403_model_cooldown_seconds()
                        extra_cooldown_until = time.time() + cooldown_secs

                # For long quota reset delays, rotate immediately instead of sleeping.
                immediate_rotate = False
                parsed_until: Optional[float] = None
                if status_code == 429 and error_text:
                    try:
                        parsed_until = await parse_and_log_cooldown(
                            error_text, mode="antigravity", retry_after_header=retry_after_header
                        )
                    except Exception:
                        parsed_until = None
                    if parsed_until is not None:
                        threshold = await get_long_quota_cooldown_rotate_threshold_seconds()
                        if (parsed_until - time.time()) > float(threshold):
                            immediate_rotate = True

                if immediate_rotate:
                    await record_api_call_error(
                        credential_manager,
                        current_file,
                        status_code,
                        parsed_until,
                        mode="antigravity",
                        model_key=model_name,
                        error_text=error_text or "",
                    )
                    if attempt < max_retries:
                        # Preheat next credential for fast failover.
                        if next_cred_task is None:
                            next_cred_task = asyncio.create_task(
                                credential_manager.get_valid_credential(
                                    mode="antigravity",
                                    model_key=model_name,
                                    exclude_filenames=list(tried_files),
                                )
                            )
                        need_retry = True
                        await asyncio.sleep((await get_retry_rotate_delay_ms()) / 1000.0)
                    else:
                        log.error(f"[ANTIGRAVITY] 达到最大重试次数 ({max_retries})，返回原始错误")
                        _log_payload_debug("ANTIGRAVITY MAX-RETRY", final_payload, status_code, current_file)
                        return last_error_response

                if not immediate_rotate and strategy != RetryStrategy.NO_RETRY:
                    # 可重试的错误 (429, 503, 529, 500, 401, 403)
                    log.warning(
                        f"[ANTIGRAVITY] 非流式请求失败 (status={status_code}), "
                        f"策略={strategy.value}, 凭证: {current_file}, "
                        f"响应: {error_text[:500] if error_text else '无'}"
                    )

                    # 并行预热下一个凭证 (仅在需要轮换账号时)
                    if should_rotate_account(status_code, error_text or ""):
                        if next_cred_task is None and attempt < max_retries:
                            next_cred_task = asyncio.create_task(
                                credential_manager.get_valid_credential(
                                    mode="antigravity",
                                    model_key=model_name,
                                    exclude_filenames=list(tried_files),
                                )
                            )

                    # 记录错误
                    cooldown_until = extra_cooldown_until
                    if cooldown_until is None and status_code == 429 and error_text:
                        try:
                            cooldown_until = await parse_and_log_cooldown(
                                error_text, mode="antigravity", retry_after_header=retry_after_header
                            )
                        except Exception:
                            pass

                    # Realtime quota refresh fallback (Antigravity-Manager style)
                    if cooldown_until is None and status_code == 429:
                        try:
                            from config import (
                                get_realtime_quota_refresh_enabled,
                                get_realtime_quota_refresh_timeout_seconds,
                                get_realtime_quota_refresh_cache_ttl_seconds,
                                get_realtime_quota_refresh_fallback_to_earliest_reset,
                            )

                            if await get_realtime_quota_refresh_enabled():
                                api_base_url = await get_antigravity_api_url()
                                cooldown_until = await fetch_realtime_quota_reset_timestamp(
                                    api_base_url=api_base_url,
                                    headers=auth_headers,
                                    model_name=str(model_name),
                                    cache_key=str(current_file),
                                    cache_ttl_seconds=int(await get_realtime_quota_refresh_cache_ttl_seconds()),
                                    timeout_seconds=float(await get_realtime_quota_refresh_timeout_seconds()),
                                    fallback_to_earliest=bool(
                                        await get_realtime_quota_refresh_fallback_to_earliest_reset()
                                    ),
                                )
                        except Exception as e:
                            log.debug(f"[ANTIGRAVITY] Realtime quota refresh failed: {e}")

                    await record_api_call_error(
                        credential_manager, current_file, status_code,
                        cooldown_until, mode="antigravity", model_key=model_name,
                        error_text=error_text or "",
                    )
                    # Auto-ban: disable credential on DISABLE_ERROR_CODES
                    if await check_should_auto_ban(status_code):
                        await handle_auto_ban(
                            credential_manager, status_code, current_file,
                            mode="antigravity", error_text=error_text or "",
                        )

                    # 应用重试延迟
                    if attempt < max_retries:
                        should_continue = await apply_retry_delay(
                            strategy, base_ms, max_ms, attempt,
                            trace_id=f"ANTIGRAVITY-{current_file[:20]}"
                        )
                        if should_continue:
                            need_retry = True
                    else:
                        # 达到最大重试次数
                        log.error(f"[ANTIGRAVITY] 达到最大重试次数 ({max_retries})，返回原始错误")
                        _log_payload_debug("ANTIGRAVITY MAX-RETRY", final_payload, status_code, current_file)
                        return last_error_response
                else:
                    # 不可重试的错误 (400等)
                    log.error(
                        f"[ANTIGRAVITY] 非流式请求失败，非重试错误码 (status={status_code}), "
                        f"凭证: {current_file}, 响应: {error_text[:500] if error_text else '无'}"
                    )
                    _log_payload_debug("ANTIGRAVITY NON-STREAM", final_payload, status_code, current_file)
                    await record_api_call_error(
                        credential_manager, current_file, status_code,
                        None, mode="antigravity", model_key=model_name,
                        error_text=error_text or "",
                    )
                    return last_error_response
            
                    # 统一处理重试
            if need_retry:
                log.info(f"[ANTIGRAVITY] 重试请求 (attempt {attempt + 2}/{max_retries + 1})...")

                if (not force_rotate_credential) and not should_rotate_account(int(status_code), error_text or ""):
                    log.info(f"[ANTIGRAVITY] Keeping same credential for status={status_code}")
                    continue

                # 使用预热的凭证任务,避免等待
                if next_cred_task is not None:
                    try:
                        cred_result = await next_cred_task
                        next_cred_task = None  # 重置任务

                        if cred_result:
                            current_file, credential_data = cred_result
                            tried_files.add(str(current_file))
                            access_token = credential_data.get("access_token") or credential_data.get("token")
                            project_id = credential_data.get("project_id", "")
                            if access_token and project_id:
                                auth_headers["Authorization"] = f"Bearer {access_token}"
                                final_payload["project"] = project_id
                                continue  # 重试
                    except Exception as e:
                        log.warning(f"[ANTIGRAVITY] 预热凭证任务失败: {e}")
                        next_cred_task = None

                # 如果预热的凭证不可用,则同步获取

                if not await refresh_credential_fast():
                    log.error("[ANTIGRAVITY] 重试时无可用凭证或令牌")
                    if await get_pool_wait_enabled():
                        try:
                            waited = await credential_manager.wait_for_valid_credential(
                                mode="antigravity",
                                model_key=model_name,
                                exclude_filenames=None,
                                max_wait_seconds=await get_pool_wait_max_seconds(),
                                poll_seconds=await get_pool_wait_poll_seconds(),
                            )
                            if waited:
                                current_file, credential_data = waited
                                tried_files.add(str(current_file))
                                access_token = credential_data.get("access_token") or credential_data.get("token")
                                project_id = credential_data.get("project_id", "")
                                if access_token and project_id:
                                    auth_headers["Authorization"] = f"Bearer {access_token}"
                                    final_payload["project"] = project_id
                                    continue
                        except Exception as e:
                            log.warning(f"[ANTIGRAVITY] Pool wait during retry failed: {e}")

                    snapshot = await credential_manager.get_model_availability_snapshot(
                        mode="antigravity", model_key=model_name, exclude_filenames=None
                    )
                    return _build_no_credentials_response(snapshot)
                continue  # 重试

        except Exception as e:
            log.error(f"[ANTIGRAVITY] 非流式请求异常: {e}, 凭证: {current_file}")
            if attempt < max_retries:
                log.info(f"[ANTIGRAVITY] 异常后重试 (attempt {attempt + 2}/{max_retries + 1})...")
                delay_ms = await get_retry_rotate_delay_ms()
                await asyncio.sleep(delay_ms / 1000.0)
                continue
            else:
                # 所有重试都失败，返回最后一次的错误（如果有）
                log.error(f"[ANTIGRAVITY] 所有重试均失败，最后异常: {e}")
                if last_error_response:
                    return last_error_response
                else:
                    return Response(
                        content=json.dumps({"error": f"非流式请求异常: {str(e)}"}),
                        status_code=500,
                        media_type="application/json"
                    )

    # 所有重试都失败，返回最后一次的原始错误
    log.error("[ANTIGRAVITY] 所有重试均失败")
    if last_error_response:
        return last_error_response
    else:
        return Response(
            content=json.dumps({"error": "所有重试均失败"}),
            status_code=500,
            media_type="application/json"
        )


# ==================== 模型和配额查询 ====================

async def fetch_available_models() -> List[Dict[str, Any]]:
    """
    获取可用模型列表，返回符合 OpenAI API 规范的格式
    
    Returns:
        模型列表，格式为字典列表（用于兼容现有代码）
        
    Raises:
        返回空列表如果获取失败
    """
    # 获取凭证管理器和可用凭证
    cred_result = await credential_manager.get_valid_credential(mode="antigravity")
    if not cred_result:
        log.error("[ANTIGRAVITY] No valid credentials available for fetching models")
        return []

    current_file, credential_data = cred_result
    access_token = credential_data.get("access_token") or credential_data.get("token")

    if not access_token:
        log.error(f"[ANTIGRAVITY] No access token in credential: {current_file}")
        return []

    # 构建请求头
    headers = build_antigravity_headers(access_token)

    try:
        # 使用 POST 请求获取模型列表
        antigravity_url = await get_antigravity_api_url()

        response = await post_async(
            url=f"{antigravity_url}/v1internal:fetchAvailableModels",
            json={},  # 空的请求体
            headers=headers
        )

        if response.status_code == 200:
            data = response.json()
            log.debug(f"[ANTIGRAVITY] Raw models response: {json.dumps(data, ensure_ascii=False)[:500]}")

            # 转换为 OpenAI 格式的模型列表，使用 Model 类
            model_list = []
            current_timestamp = int(datetime.now(timezone.utc).timestamp())

            if 'models' in data and isinstance(data['models'], dict):
                # 遍历模型字典
                for model_id in data['models'].keys():
                    model = Model(
                        id=model_id,
                        object='model',
                        created=current_timestamp,
                        owned_by='google'
                    )
                    model_list.append(model_to_dict(model))

            if "claude-opus-4-5-thinking" in data.get('models', {}):
                # 添加 claude-opus-4-5 模型
                model = Model(
                    id='claude-opus-4-5',
                    object='model',
                    created=current_timestamp,
                    owned_by='google'
                )
                model_list.append(model_to_dict(model))

            # 添加额外的 claude-opus-4-6 模型
            if "claude-opus-4-6-thinking" in data.get('models', {}):
                claude_opus_model = Model(
                    id='claude-opus-4-6',
                    object='model',
                    created=current_timestamp,
                    owned_by='google'
                )
                model_list.append(model_to_dict(claude_opus_model))

            log.info(f"[ANTIGRAVITY] Fetched {len(model_list)} available models")
            return model_list
        else:
            log.error(f"[ANTIGRAVITY] Failed to fetch models ({response.status_code}): {response.text[:500]}")
            return []

    except Exception as e:
        import traceback
        log.error(f"[ANTIGRAVITY] Failed to fetch models: {e}")
        log.error(f"[ANTIGRAVITY] Traceback: {traceback.format_exc()}")
        return []


async def fetch_quota_info(access_token: str) -> Dict[str, Any]:
    """
    获取指定凭证的额度信息
    
    Args:
        access_token: Antigravity 访问令牌
        
    Returns:
        包含额度信息的字典，格式为：
        {
            "success": True/False,
            "models": {
                "model_name": {
                    "remaining": 0.95,
                    "resetTime": "12-20 09:30",      # server-local display string
                    "resetTimeRaw": "2025-12-20T02:30:00Z"  # authoritative RFC3339 timestamp
                }
            },
            "error": "错误信息" (仅在失败时)
        }
    """

    headers = build_antigravity_headers(access_token)

    try:
        antigravity_url = await get_antigravity_api_url()

        response = await post_async(
            url=f"{antigravity_url}/v1internal:fetchAvailableModels",
            json={},
            headers=headers,
            timeout=30.0
        )

        if response.status_code == 200:
            data = response.json()
            log.debug(f"[ANTIGRAVITY QUOTA] Raw response: {json.dumps(data, ensure_ascii=False)[:500]}")

            quota_info = {}

            if 'models' in data and isinstance(data['models'], dict):
                for model_id, model_data in data['models'].items():
                    if isinstance(model_data, dict) and 'quotaInfo' in model_data:
                        quota = model_data['quotaInfo']
                        remaining = quota.get('remainingFraction', 0)
                        reset_time_raw = quota.get('resetTime', '')

                        # Display reset time in server-local timezone.
                        # Note: resetTimeRaw is the authoritative RFC3339 timestamp from upstream.
                        reset_time_local = "N/A"
                        if reset_time_raw:
                            try:
                                utc_date = datetime.fromisoformat(reset_time_raw.replace("Z", "+00:00"))
                                local_date = utc_date.astimezone()
                                reset_time_local = local_date.strftime("%m-%d %H:%M")
                            except Exception as e:
                                log.warning(f"[ANTIGRAVITY QUOTA] Failed to parse reset time: {e}")

                        quota_info[model_id] = {
                            "remaining": remaining,
                            "resetTime": reset_time_local,
                            "resetTimeRaw": reset_time_raw,
                        }

            return {
                "success": True,
                "models": quota_info
            }
        else:
            log.error(f"[ANTIGRAVITY QUOTA] Failed to fetch quota ({response.status_code}): {response.text[:500]}")
            return {
                "success": False,
                "error": f"API返回错误: {response.status_code}"
            }

    except Exception as e:
        import traceback
        log.error(f"[ANTIGRAVITY QUOTA] Failed to fetch quota: {e}")
        log.error(f"[ANTIGRAVITY QUOTA] Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "error": str(e)
        }
