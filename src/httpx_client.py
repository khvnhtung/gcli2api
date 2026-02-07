"""
通用的HTTP客户端模块
为所有需要使用httpx的模块提供统一的客户端配置和方法
保持通用性，不与特定业务逻辑耦合
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

from config import get_proxy_config
from log import log


class HttpxClientManager:
    """通用HTTP客户端管理器"""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._client_proxy: Optional[str] = None

    async def get_client_kwargs(self, timeout: float = 30.0, **kwargs) -> Dict[str, Any]:
        """获取httpx客户端的通用配置参数"""
        client_kwargs = {"timeout": timeout, **kwargs}

        # 动态读取代理配置，支持热更新
        current_proxy_config = await get_proxy_config()
        if current_proxy_config:
            client_kwargs["proxy"] = current_proxy_config

        return client_kwargs

    async def _get_shared_client(self) -> httpx.AsyncClient:
        """获取或创建共享的httpx客户端"""
        current_proxy = await get_proxy_config()

        # 如果代理配置变更，需要重建客户端
        if self._client and self._client_proxy != current_proxy:
            await self._client.aclose()
            self._client = None

        if self._client is None or self._client.is_closed:
            kwargs = {}
            if current_proxy:
                kwargs["proxy"] = current_proxy

            # 设置合理的连接池限制
            # max_keepalive_connections: 保持的空闲连接数
            # max_connections: 最大并发连接数
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)

            # 默认超时设置，请求时可覆盖
            self._client = httpx.AsyncClient(limits=limits, timeout=60.0, **kwargs)
            self._client_proxy = current_proxy

        return self._client

    @asynccontextmanager
    async def get_client(
        self, timeout: float = 30.0, **kwargs
    ) -> AsyncGenerator[httpx.AsyncClient, None]:
        """获取配置好的异步HTTP客户端"""
        # 使用共享客户端
        client = await self._get_shared_client()
        yield client

    @asynccontextmanager
    async def get_streaming_client(
        self, timeout: Optional[float] = None, **kwargs
    ) -> AsyncGenerator[httpx.AsyncClient, None]:
        """获取用于流式请求的HTTP客户端"""
        # 流式请求也复用同一个客户端
        client = await self._get_shared_client()
        yield client


# 全局HTTP客户端管理器实例
http_client = HttpxClientManager()


# 通用的异步方法
async def get_async(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = 30.0, **kwargs
) -> httpx.Response:
    """通用异步GET请求"""
    async with http_client.get_client(**kwargs) as client:
        # 显式传递timeout
        return await client.get(url, headers=headers, timeout=timeout)


async def post_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = 600.0,
    **kwargs,
) -> httpx.Response:
    """通用异步POST请求"""
    async with http_client.get_client(**kwargs) as client:
        # 显式传递timeout
        return await client.post(url, data=data, json=json, headers=headers, timeout=timeout)


async def stream_post_async(
    url: str,
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,  # 添加timeout参数
    **kwargs,
):
    """流式异步POST请求"""
    async with http_client.get_streaming_client(**kwargs) as client:
        # 显式传递timeout (None表示无超时)
        async with client.stream("POST", url, json=body, headers=headers, timeout=timeout) as r:
            # 错误直接返回
            if r.status_code != 200:
                from fastapi import Response
                yield Response(await r.aread(), r.status_code, dict(r.headers))
                return

            # 如果native=True，直接返回bytes流
            if native:
                async for chunk in r.aiter_bytes():
                    yield chunk
            else:
                # 通过aiter_lines转化成str流返回
                async for line in r.aiter_lines():
                    yield line
