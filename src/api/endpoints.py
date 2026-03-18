"""API 端点

定义代理服务的所有 HTTP 端点：
- POST /v1/responses — 主端点，转换 Responses API 请求
- GET /health — 健康检查
- GET / — 根端点，返回服务信息
"""

from fastapi import APIRouter, HTTPException, Request, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from datetime import datetime
import json
import uuid
import traceback
from typing import Optional

from src.core.config import config
from src.core.logging import logger
from src.core.client import OpenAIClient
from src.models.responses import ResponsesRequest
from src.conversion.request_converter import convert_responses_to_chat_completion
from src.conversion.response_converter import (
    convert_chat_completion_to_responses,
    convert_chat_stream_to_responses_sse,
)

router = APIRouter()

# 获取自定义请求头
custom_headers = config.get_custom_headers()

# 创建 OpenAI 客户端
openai_client = OpenAIClient(
    config.openai_api_key,
    config.openai_base_url,
    config.request_timeout,
    config.read_timeout,
    custom_headers=custom_headers,
)


async def validate_api_key(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """验证客户端 API 密钥

    支持两种认证方式：
    - x-api-key 请求头
    - Authorization: Bearer <key> 请求头
    """
    client_api_key = None

    # 从请求头中提取 API 密钥
    if x_api_key:
        client_api_key = x_api_key
    elif authorization and authorization.startswith("Bearer "):
        client_api_key = authorization.replace("Bearer ", "")

    # 未设置 PROXY_API_KEY 时跳过验证
    if not config.client_api_key:
        return

    # 验证客户端 API 密钥
    if not client_api_key or not config.validate_client_api_key(client_api_key):
        logger.warning("客户端提供了无效的 API 密钥")
        raise HTTPException(
            status_code=401,
            detail="Invalid API key. Please provide a valid Proxy API key.",
        )


@router.post("/v1/responses")
async def create_response(
    request: ResponsesRequest,
    http_request: Request,
    _: None = Depends(validate_api_key),
):
    """Responses API 主端点

    接收 OpenAI Responses API 格式的请求，
    转换为 Chat Completions 格式转发给上游，
    再将响应转换回 Responses API 格式返回。
    """
    try:
        logger.debug(
            f"处理 Responses API 请求: model={request.model}, stream={request.stream}"
        )

        # 生成唯一请求 ID，用于取消追踪
        request_id = str(uuid.uuid4())

        # 记录原始请求（用于调试）
        logger.info(f"原始 Responses API 请求: model={request.model}, stream={request.stream}, "
                     f"tools_count={len(request.tools) if request.tools else 0}, "
                     f"input_type={'str' if isinstance(request.input, str) else 'list'}")
        if request.tools:
            for idx, tool in enumerate(request.tools):
                logger.info(f"  tool[{idx}]: type={tool.get('type')}, name={tool.get('name')}, "
                            f"keys={list(tool.keys())}")
        if isinstance(request.input, list):
            for idx, item in enumerate(request.input):
                if isinstance(item, dict):
                    logger.info(f"  input[{idx}]: type={item.get('type')}, role={item.get('role')}, "
                                f"keys={list(item.keys())}")

        # 转换请求格式
        chat_request = convert_responses_to_chat_completion(request)

        logger.debug(f"转换后的 Chat Completions 请求: model={chat_request.get('model')}, "
                     f"stream={chat_request.get('stream')}, "
                     f"messages_count={len(chat_request.get('messages', []))}, "
                     f"tools_count={len(chat_request.get('tools', []))}, "
                     f"reasoning_effort={chat_request.get('reasoning_effort', 'N/A')}")

        # 检查客户端是否已断开
        if await http_request.is_disconnected():
            raise HTTPException(status_code=499, detail="Client disconnected")

        if request.stream:
            # 流式响应
            try:
                openai_stream = openai_client.create_chat_completion_stream(
                    chat_request, request_id
                )
                return StreamingResponse(
                    convert_chat_stream_to_responses_sse(
                        openai_stream,
                        request,
                        http_request,
                        openai_client,
                        request_id,
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "*",
                    },
                )
            except HTTPException as e:
                # 流式请求的错误转换为标准错误响应
                logger.error(f"流式请求错误: {e.detail}")
                logger.error(traceback.format_exc())
                error_message = openai_client.classify_openai_error(e.detail)
                error_response = {
                    "type": "error",
                    "error": {"type": "api_error", "message": error_message},
                }
                return JSONResponse(status_code=e.status_code, content=error_response)
        else:
            # 非流式响应
            openai_response = await openai_client.create_chat_completion(
                chat_request, request_id
            )
            responses_result = convert_chat_completion_to_responses(
                openai_response, request
            )
            return responses_result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理请求时发生意外错误: {e}")
        logger.error(traceback.format_exc())
        error_message = openai_client.classify_openai_error(str(e))
        raise HTTPException(status_code=500, detail=error_message)


@router.get("/health")
async def health_check():
    """健康检查端点"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "openai_api_configured": bool(config.openai_api_key),
        "client_api_key_validation": bool(config.client_api_key),
    }


@router.get("/")
async def root():
    """根端点，返回服务信息"""
    return {
        "message": "Codex Proxy v1.0.0 — Responses API → Chat Completions Proxy",
        "status": "running",
        "config": {
            "openai_base_url": config.openai_base_url,
            "api_key_configured": bool(config.openai_api_key),
            "client_api_key_validation": bool(config.client_api_key),
        },
        "endpoints": {
            "responses": "/v1/responses",
            "health": "/health",
        },
    }
