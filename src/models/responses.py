"""Responses API 数据模型

定义 Codex CLI 发送的 Responses API 请求的 Pydantic 模型。
"""

from typing import Any, Optional, Union
from pydantic import BaseModel, ConfigDict


class ResponsesRequest(BaseModel):
    """OpenAI Responses API 请求模型

    对应 POST /v1/responses 的请求体。
    input 可以是字符串（简单文本）或 input items 数组（多轮对话）。
    """

    model: str
    input: Union[str, list[Any]]  # 字符串或 input items 数组
    instructions: Optional[str] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stream: Optional[bool] = False
    tools: Optional[list[dict]] = None
    tool_choice: Optional[Any] = None
    # o 系列模型的推理配置，如 {"effort": "high"}
    reasoning: Optional[dict] = None
    # 是否允许模型并行调用多个工具
    parallel_tool_calls: Optional[bool] = None
    # Codex CLI 可能发送的额外字段（允许透传）
    model_config = ConfigDict(extra="allow")
