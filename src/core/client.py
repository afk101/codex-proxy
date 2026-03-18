import asyncio
import json
import logging
import traceback
import httpx
from fastapi import HTTPException
from typing import Optional, AsyncGenerator, Dict, Any
from openai import AsyncOpenAI
from openai._exceptions import APIError, RateLimitError, AuthenticationError, BadRequestError
from src.core.constants import Constants

logger = logging.getLogger(__name__)


class OpenAIClient:
    """异步 OpenAI 客户端，支持请求取消。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: int = Constants.DEFAULT_REQUEST_TIMEOUT,
        read_timeout: int = Constants.DEFAULT_READ_TIMEOUT,
        custom_headers: Optional[Dict[str, str]] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.custom_headers = custom_headers or {}

        # 分别设置连接超时和读取超时，避免流式传输中途超时
        request_timeout = httpx.Timeout(
            connect=timeout,
            read=read_timeout,
            write=timeout,
            pool=timeout,
        )

        # 默认请求头
        default_headers = {
            "Content-Type": "application/json",
            "User-Agent": Constants.USER_AGENT,
        }

        # 合并自定义请求头
        all_headers = {**default_headers, **self.custom_headers}

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=request_timeout,
            default_headers=all_headers,
        )
        # 活跃请求追踪，用于取消支持
        self.active_requests: Dict[str, asyncio.Event] = {}

    async def create_chat_completion(
        self, request: Dict[str, Any], request_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """发送非流式 Chat Completion 请求，支持取消。"""

        # 创建取消令牌
        if request_id:
            cancel_event = asyncio.Event()
            self.active_requests[request_id] = cancel_event

        try:
            # 创建可取消的任务
            completion_task = asyncio.create_task(
                self.client.chat.completions.create(**request)
            )

            if request_id:
                # 等待完成或取消
                cancel_task = asyncio.create_task(cancel_event.wait())
                done, pending = await asyncio.wait(
                    [completion_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # 取消未完成的任务
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # 检查是否被客户端取消
                if cancel_task in done:
                    completion_task.cancel()
                    raise HTTPException(status_code=499, detail="Request cancelled by client")

                completion = await completion_task
            else:
                completion = await completion_task

            # 转换为字典格式
            return completion.model_dump()

        except AuthenticationError as e:
            logger.error(f"上游API认证错误: {type(e).__name__}: {e}")
            raise HTTPException(status_code=401, detail=self.classify_openai_error(str(e)))
        except RateLimitError as e:
            logger.error(f"上游API限流: {type(e).__name__}: {e}")
            raise HTTPException(status_code=429, detail=self.classify_openai_error(str(e)))
        except BadRequestError as e:
            logger.error(f"上游API请求错误: {type(e).__name__}: {e}")
            raise HTTPException(status_code=400, detail=self.classify_openai_error(str(e)))
        except APIError as e:
            status_code = getattr(e, 'status_code', 500)
            logger.error(f"上游API错误: {type(e).__name__}(status={status_code}): {e}")
            raise HTTPException(status_code=status_code, detail=self.classify_openai_error(str(e)))
        except Exception as e:
            logger.error(f"非预期错误: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

        finally:
            # 清理活跃请求追踪
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]

    async def create_chat_completion_stream(
        self, request: Dict[str, Any], request_id: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """发送流式 Chat Completion 请求，支持取消。"""

        # 创建取消令牌
        if request_id:
            cancel_event = asyncio.Event()
            self.active_requests[request_id] = cancel_event

        try:
            # 确保启用流式传输
            request["stream"] = True
            if "stream_options" not in request:
                request["stream_options"] = {}
            request["stream_options"]["include_usage"] = True

            # 创建流式 completion
            streaming_completion = await self.client.chat.completions.create(**request)

            async for chunk in streaming_completion:
                # 每个 chunk 前检查是否已取消
                if request_id and request_id in self.active_requests:
                    if self.active_requests[request_id].is_set():
                        raise HTTPException(status_code=499, detail="Request cancelled by client")

                # 转换 chunk 为 SSE 格式
                chunk_dict = chunk.model_dump()
                chunk_json = json.dumps(chunk_dict, ensure_ascii=False)
                yield f"data: {chunk_json}"

            # 流结束信号
            yield "data: [DONE]"

        except AuthenticationError as e:
            logger.error(f"上游API认证错误(流式): {type(e).__name__}: {e}")
            raise HTTPException(status_code=401, detail=self.classify_openai_error(str(e)))
        except RateLimitError as e:
            logger.error(f"上游API限流(流式): {type(e).__name__}: {e}")
            raise HTTPException(status_code=429, detail=self.classify_openai_error(str(e)))
        except BadRequestError as e:
            logger.error(f"上游API请求错误(流式): {type(e).__name__}: {e}")
            raise HTTPException(status_code=400, detail=self.classify_openai_error(str(e)))
        except APIError as e:
            status_code = getattr(e, 'status_code', 500)
            logger.error(f"上游API错误(流式): {type(e).__name__}(status={status_code}): {e}")
            raise HTTPException(status_code=status_code, detail=self.classify_openai_error(str(e)))
        except Exception as e:
            logger.error(f"非预期错误(流式): {type(e).__name__}: {e}\n{traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

        finally:
            # 清理活跃请求追踪
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]

    def classify_openai_error(self, error_detail: Any) -> str:
        """对常见 OpenAI API 错误提供具体的错误提示。"""
        error_str = str(error_detail).lower()

        # 地区限制
        if "unsupported_country_region_territory" in error_str or "country, region, or territory not supported" in error_str:
            return "OpenAI API is not available in your region. Consider using a VPN or alternative provider."

        # API 密钥问题
        if "invalid_api_key" in error_str or "unauthorized" in error_str:
            return "Invalid API key. Please check your OPENAI_API_KEY configuration."

        # 限流
        if "rate_limit" in error_str or "quota" in error_str:
            return "Rate limit exceeded. Please wait and try again, or upgrade your API plan."

        # 模型未找到
        if "model" in error_str and ("not found" in error_str or "does not exist" in error_str):
            return "Model not found. Please check your model configuration."

        # 账单问题
        if "billing" in error_str or "payment" in error_str:
            return "Billing issue. Please check your account billing status."

        # 默认: 返回原始消息
        return str(error_detail)

    def cancel_request(self, request_id: str) -> bool:
        """通过 request_id 取消活跃请求。"""
        if request_id in self.active_requests:
            self.active_requests[request_id].set()
            return True
        return False
