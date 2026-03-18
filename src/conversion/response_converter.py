"""响应转换器

将 Chat Completions API 响应转换为 Responses API 响应格式。
包含非流式和流式 SSE 转换。
"""

import json
import logging
import time
import uuid
import traceback
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional
from fastapi import Request
from src.core.constants import Constants
from src.core.client import OpenAIClient
from src.models.responses import ResponsesRequest

logger = logging.getLogger(__name__)


# ========== 非流式转换 ==========


def convert_chat_completion_to_responses(
    chat_response: dict, original_request: ResponsesRequest
) -> dict:
    """将 Chat Completions 响应转换为 Responses API 响应

    Args:
        chat_response: Chat Completions API 的响应字典
        original_request: 原始的 Responses API 请求

    Returns:
        Responses API 格式的响应字典
    """
    response_id = _generate_response_id()

    choices = chat_response.get("choices", [])
    if not choices:
        # 空响应
        return _build_response_object_dict(
            response_id=response_id,
            model=original_request.model,
            status=Constants.STATUS_COMPLETED,
            output=[],
            usage=_convert_usage(chat_response.get("usage")),
        )

    choice = choices[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", Constants.FINISH_STOP)

    output = []

    # 处理文本内容和 refusal
    # Responses API 规范中 content 和 refusal 应属于同一个 message 的不同 content block
    content = message.get("content")
    refusal = message.get("refusal")

    if content or refusal:
        # 将 content 和 refusal 合并到同一个 message output item 的 content 数组中
        output.append(_build_message_output_item_with_refusal(content, refusal))

    # 处理 reasoning（o 系列推理模型的推理内容）
    # 不同提供商可能使用 "reasoning" 或 "reasoning_content" 字段名
    reasoning_text = message.get("reasoning") or message.get("reasoning_content")
    if reasoning_text:
        output.append(_build_reasoning_output_item(reasoning_text))

    # 处理工具调用
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        func = tc.get("function", {})
        output.append({
            "type": Constants.FUNCTION_CALL,
            "id": f"fc_{uuid.uuid4().hex[:24]}",
            "call_id": tc.get("id", ""),
            "name": func.get("name", ""),
            "arguments": func.get("arguments", "{}"),
            "status": Constants.STATUS_COMPLETED,
        })

    # 映射 finish_reason 到 status
    status, incomplete_details = _map_finish_reason_to_status(finish_reason)

    response = _build_response_object_dict(
        response_id=response_id,
        model=original_request.model,
        status=status,
        output=output,
        usage=_convert_usage(chat_response.get("usage")),
        incomplete_details=incomplete_details,
    )

    return response


# ========== 流式 SSE 转换 ==========


@dataclass
class ToolBlockState:
    """工具调用块的状态"""

    output_index: int = -1
    call_id: str = ""
    name: str = ""
    accumulated_args: str = ""
    started: bool = False  # 延迟启动：等 id+name 都就绪
    item_id: str = ""  # 首次生成的 function_call item ID，确保 added/done 事件一致


@dataclass
class StreamState:
    """流式转换的状态机"""

    response_id: str = ""
    model: str = ""
    next_output_index: int = 0
    # 文本消息状态
    text_message_opened: bool = False
    text_content_part_opened: bool = False
    accumulated_text: str = ""
    text_output_index: int = -1
    text_message_id: str = ""  # 首次生成的 message item ID，确保 added/done 事件一致
    # refusal 状态（模型拒绝响应时的流式数据）
    refusal_opened: bool = False
    accumulated_refusal: str = ""
    # reasoning 状态（o 系列推理模型的推理内容）
    reasoning_opened: bool = False
    reasoning_output_index: int = -1
    accumulated_reasoning: str = ""
    reasoning_item_id: str = ""  # 首次生成的 reasoning item ID，确保 added/done 事件一致
    # 工具调用状态（key: tool_calls 中的 index）
    tool_blocks: dict = field(default_factory=dict)
    # 最终输出
    collected_output_items: list = field(default_factory=list)
    final_usage: Optional[dict] = None


async def convert_chat_stream_to_responses_sse(
    openai_stream: AsyncGenerator[str, None],
    request: ResponsesRequest,
    http_request: Request,
    client: OpenAIClient,
    request_id: str,
) -> AsyncGenerator[str, None]:
    """将 Chat Completions 流式响应转换为 Responses API SSE 事件流

    这是核心的流式转换函数，实现了完整的状态机。

    Args:
        openai_stream: 上游 Chat Completions 的 SSE 数据流
        request: 原始的 Responses API 请求
        http_request: FastAPI 的 HTTP 请求对象（用于检测断开）
        client: OpenAI 客户端（用于取消请求）
        request_id: 请求 ID（用于取消）

    Yields:
        Responses API 格式的 SSE 事件字符串
    """
    state = StreamState(
        response_id=_generate_response_id(),
        model=request.model,
    )

    first_chunk_received = False
    finish_reason_received = None

    try:
        async for line in openai_stream:
            # 检查客户端是否断开连接
            if await http_request.is_disconnected():
                logger.info(f"客户端断开连接，取消上游请求: {request_id}")
                client.cancel_request(request_id)
                return

            # 解析 SSE data 行
            if not line.startswith("data: "):
                continue

            data_str = line[6:].strip()

            # 处理流结束信号
            if data_str == "[DONE]":
                # 如果还没有收到 finish_reason，强制关闭
                if finish_reason_received is None:
                    finish_reason_received = Constants.FINISH_STOP
                # 发送关闭事件
                async for event in _handle_finish(state, finish_reason_received):
                    yield event
                return

            # 解析 JSON chunk
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                logger.warning(f"无法解析 SSE chunk: {data_str[:100]}")
                continue

            # 提取 usage（可能在任何 chunk 中）
            if chunk.get("usage"):
                state.final_usage = chunk["usage"]

            # 获取 choices
            choices = chunk.get("choices", [])
            if not choices:
                # 纯 usage chunk，跳过
                continue

            choice = choices[0]
            delta = choice.get("delta", {})

            # 首个 chunk - 发送 response.created 和 response.in_progress
            if not first_chunk_received:
                first_chunk_received = True
                # 更新模型名称（如果上游返回了实际模型名）
                if chunk.get("model"):
                    state.model = chunk["model"]

                for event in _emit_response_created_events(state):
                    yield event

            # 处理文本增量
            content = delta.get("content")
            if content is not None and content != "":
                for event in _handle_text_delta(state, content):
                    yield event

            # 处理 refusal 增量（模型拒绝响应时的流式数据）
            refusal = delta.get("refusal")
            if refusal is not None and refusal != "":
                for event in _handle_refusal_delta(state, refusal):
                    yield event

            # 处理 reasoning 增量（o 系列推理模型的推理内容）
            # 不同提供商可能使用 "reasoning" 或 "reasoning_content" 字段名
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning is not None and reasoning != "":
                for event in _handle_reasoning_delta(state, reasoning):
                    yield event

            # 处理工具调用增量
            tool_calls = delta.get("tool_calls")
            if tool_calls:
                for tc_delta in tool_calls:
                    for event in _handle_tool_call_delta(state, tc_delta):
                        yield event

            # 检查 finish_reason
            fr = choice.get("finish_reason")
            if fr:
                finish_reason_received = fr

        # 如果循环正常结束但没有收到 [DONE]
        if finish_reason_received is None:
            finish_reason_received = Constants.FINISH_STOP
        async for event in _handle_finish(state, finish_reason_received):
            yield event

    except Exception as e:
        logger.error(f"流式转换错误: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        # 发送错误事件
        error_event = {
            "type": "error",
            "error": {
                "type": "server_error",
                "message": f"Proxy streaming error: {str(e)}",
            },
        }
        yield _emit_sse_event("error", error_event)


# ========== SSE 事件构建辅助函数 ==========


def _emit_sse_event(event_type: str, data: Any) -> str:
    """格式化 SSE 事件

    Codex CLI 从 data JSON 的 "type" 字段读取事件类型（而非 SSE event: 行），
    所以必须确保 data 中包含 "type" 字段。

    Args:
        event_type: 事件类型
        data: 事件数据

    Returns:
        格式化的 SSE 字符串
    """
    # 确保 data 中包含 type 字段（Codex CLI 依赖此字段识别事件类型）
    if isinstance(data, dict) and "type" not in data:
        data = {"type": event_type, **data}
    json_str = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {json_str}\n\n"


def _emit_response_created_events(state: StreamState) -> list[str]:
    """生成 response.created 和 response.in_progress 事件

    Codex CLI 期望 data JSON 格式为:
    {"type": "response.created", "response": {...response对象...}}

    Args:
        state: 流式状态

    Returns:
        SSE 事件列表
    """
    events = []

    # 构建初始 response 对象
    response_obj = _build_response_object_dict(
        response_id=state.response_id,
        model=state.model,
        status=Constants.STATUS_IN_PROGRESS,
        output=[],
        usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    )

    # response.created — response 嵌套在 "response" 字段中
    events.append(_emit_sse_event(
        Constants.EVENT_RESPONSE_CREATED,
        {"response": response_obj},
    ))

    # response.in_progress
    events.append(_emit_sse_event(
        Constants.EVENT_RESPONSE_IN_PROGRESS,
        {"response": response_obj},
    ))

    return events


def _handle_text_delta(state: StreamState, text: str) -> list[str]:
    """处理文本增量

    包含首次开启消息和内容块的逻辑。

    Args:
        state: 流式状态
        text: 文本增量

    Returns:
        SSE 事件列表
    """
    events = []

    # 首次收到文本 - 开启 message 和 content_part
    if not state.text_message_opened:
        state.text_output_index = state.next_output_index
        state.next_output_index += 1
        state.text_message_opened = True
        # 生成并保存 message ID，确保 added/done 事件中 ID 一致
        state.text_message_id = f"msg_{uuid.uuid4().hex[:24]}"

        # response.output_item.added - 添加 message output item
        message_item = {
            "type": "message",
            "id": state.text_message_id,
            "status": Constants.STATUS_IN_PROGRESS,
            "role": Constants.ROLE_ASSISTANT,
            "content": [],
        }
        events.append(_emit_sse_event(
            Constants.EVENT_OUTPUT_ITEM_ADDED,
            {
                "output_index": state.text_output_index,
                "item": message_item,
            },
        ))

    if not state.text_content_part_opened:
        state.text_content_part_opened = True

        # response.content_part.added
        content_part = {
            "type": Constants.OUTPUT_TEXT,
            "text": "",
            "annotations": [],
        }
        events.append(_emit_sse_event(
            Constants.EVENT_CONTENT_PART_ADDED,
            {
                "output_index": state.text_output_index,
                "content_index": 0,
                "part": content_part,
            },
        ))

    # 累积文本
    state.accumulated_text += text

    # response.output_text.delta
    events.append(_emit_sse_event(
        Constants.EVENT_OUTPUT_TEXT_DELTA,
        {
            "output_index": state.text_output_index,
            "content_index": 0,
            "delta": text,
        },
    ))

    return events


def _handle_refusal_delta(state: StreamState, refusal: str) -> list[str]:
    """处理 refusal 增量

    当模型在流式响应中拒绝时，delta 中会包含 refusal 字段。
    复用 text message 的 output item（同一个 message），
    但使用 refusal 类型的内容块。

    Args:
        state: 流式状态
        refusal: refusal 增量文本

    Returns:
        SSE 事件列表
    """
    events = []

    # 首次收到 refusal — 开启 message（如果还没开启的话）
    if not state.text_message_opened:
        state.text_output_index = state.next_output_index
        state.next_output_index += 1
        state.text_message_opened = True
        # 生成并保存 message ID，确保 added/done 事件中 ID 一致
        state.text_message_id = f"msg_{uuid.uuid4().hex[:24]}"

        # response.output_item.added — 添加 message output item
        message_item = {
            "type": "message",
            "id": state.text_message_id,
            "status": Constants.STATUS_IN_PROGRESS,
            "role": Constants.ROLE_ASSISTANT,
            "content": [],
        }
        events.append(_emit_sse_event(
            Constants.EVENT_OUTPUT_ITEM_ADDED,
            {
                "output_index": state.text_output_index,
                "item": message_item,
            },
        ))

    if not state.refusal_opened:
        state.refusal_opened = True

        # refusal 的 content_index：如果已有文本块则为 1，否则为 0
        refusal_content_index = 1 if state.text_content_part_opened else 0

        # response.content_part.added — refusal 类型
        content_part = {
            "type": Constants.REFUSAL,
            "refusal": "",
        }
        events.append(_emit_sse_event(
            Constants.EVENT_CONTENT_PART_ADDED,
            {
                "output_index": state.text_output_index,
                "content_index": refusal_content_index,
                "part": content_part,
            },
        ))

    # 累积 refusal
    state.accumulated_refusal += refusal

    # response.refusal.delta
    refusal_content_index = 1 if state.text_content_part_opened else 0
    events.append(_emit_sse_event(
        Constants.EVENT_REFUSAL_DELTA,
        {
            "output_index": state.text_output_index,
            "content_index": refusal_content_index,
            "delta": refusal,
        },
    ))

    return events


def _handle_reasoning_delta(state: StreamState, reasoning: str) -> list[str]:
    """处理 reasoning 增量（o 系列推理模型的推理内容）

    将 Chat Completions 流式响应中的 delta.reasoning 内容转换为
    Responses API 的 reasoning 事件。reasoning 作为独立的 output item
    发送，与文本消息分离。

    Args:
        state: 流式状态
        reasoning: reasoning 增量文本

    Returns:
        SSE 事件列表
    """
    events = []

    # 首次收到 reasoning — 开启 reasoning output item
    if not state.reasoning_opened:
        state.reasoning_output_index = state.next_output_index
        state.next_output_index += 1
        state.reasoning_opened = True
        # 生成并保存 reasoning item ID，确保 added/done 事件中 ID 一致
        state.reasoning_item_id = f"rs_{uuid.uuid4().hex[:24]}"

        # response.output_item.added — reasoning 类型
        reasoning_item = {
            "type": Constants.REASONING,
            "id": state.reasoning_item_id,
            "summary": [],
        }
        events.append(_emit_sse_event(
            Constants.EVENT_OUTPUT_ITEM_ADDED,
            {
                "output_index": state.reasoning_output_index,
                "item": reasoning_item,
            },
        ))

    # 累积 reasoning 文本
    state.accumulated_reasoning += reasoning

    # response.reasoning.delta
    events.append(_emit_sse_event(
        Constants.EVENT_REASONING_DELTA,
        {
            "output_index": state.reasoning_output_index,
            "delta": reasoning,
        },
    ))

    return events


def _handle_tool_call_delta(state: StreamState, tc_delta: dict) -> list[str]:
    """处理工具调用增量

    实现延迟启动：等 id 和 name 都就绪才发送 output_item.added。
    在此之前的 arguments 暂存。

    Args:
        state: 流式状态
        tc_delta: 工具调用增量字典

    Returns:
        SSE 事件列表
    """
    events = []
    tc_index = tc_delta.get("index", 0)

    # 获取或创建工具块状态
    if tc_index not in state.tool_blocks:
        state.tool_blocks[tc_index] = ToolBlockState()

    block = state.tool_blocks[tc_index]

    # 更新 id
    if tc_delta.get("id"):
        block.call_id = tc_delta["id"]

    # 更新 name
    func = tc_delta.get("function", {})
    if func.get("name"):
        block.name = func["name"]

    # 检查是否应该启动（id 和 name 都就绪）
    should_start = not block.started and block.call_id and block.name
    if should_start:
        block.started = True
        block.output_index = state.next_output_index
        state.next_output_index += 1
        # 生成并保存 function_call item ID，确保 added/done 事件中 ID 一致
        block.item_id = f"fc_{uuid.uuid4().hex[:24]}"

        # response.output_item.added - function_call
        fc_item = {
            "type": Constants.FUNCTION_CALL,
            "id": block.item_id,
            "call_id": block.call_id,
            "name": block.name,
            "arguments": "",
            "status": Constants.STATUS_IN_PROGRESS,
        }
        events.append(_emit_sse_event(
            Constants.EVENT_OUTPUT_ITEM_ADDED,
            {
                "output_index": block.output_index,
                "item": fc_item,
            },
        ))

        # 发送之前暂存的 arguments
        if block.accumulated_args:
            events.append(_emit_sse_event(
                Constants.EVENT_FUNCTION_CALL_ARGS_DELTA,
                {
                    "output_index": block.output_index,
                    "call_id": block.call_id,
                    "delta": block.accumulated_args,
                },
            ))

    # 处理 arguments 增量
    args_delta = func.get("arguments")
    if args_delta:
        block.accumulated_args += args_delta
        if block.started:
            # 已启动，直接发送
            events.append(_emit_sse_event(
                Constants.EVENT_FUNCTION_CALL_ARGS_DELTA,
                {
                    "output_index": block.output_index,
                    "call_id": block.call_id,
                    "delta": args_delta,
                },
            ))
        # 未启动的情况下，arguments 已经在上面累积到 accumulated_args 中

    return events


async def _handle_finish(state: StreamState, finish_reason: str) -> AsyncGenerator[str, None]:
    """处理流结束

    关闭所有打开的块，发送 done 事件和 response.completed。

    Args:
        state: 流式状态
        finish_reason: Chat Completions 的 finish_reason

    Yields:
        SSE 事件字符串
    """
    output_items = []

    # 关闭文本块
    if state.text_content_part_opened:
        # response.output_text.done
        yield _emit_sse_event(
            Constants.EVENT_OUTPUT_TEXT_DONE,
            {
                "output_index": state.text_output_index,
                "content_index": 0,
                "text": state.accumulated_text,
            },
        )

        # response.content_part.done
        yield _emit_sse_event(
            Constants.EVENT_CONTENT_PART_DONE,
            {
                "output_index": state.text_output_index,
                "content_index": 0,
                "part": {
                    "type": Constants.OUTPUT_TEXT,
                    "text": state.accumulated_text,
                    "annotations": [],
                },
            },
        )

    # 关闭 refusal 块
    if state.refusal_opened:
        refusal_content_index = 1 if state.text_content_part_opened else 0

        # response.refusal.done
        yield _emit_sse_event(
            Constants.EVENT_REFUSAL_DONE,
            {
                "output_index": state.text_output_index,
                "content_index": refusal_content_index,
                "refusal": state.accumulated_refusal,
            },
        )

        # response.content_part.done — refusal 类型
        yield _emit_sse_event(
            Constants.EVENT_CONTENT_PART_DONE,
            {
                "output_index": state.text_output_index,
                "content_index": refusal_content_index,
                "part": {
                    "type": Constants.REFUSAL,
                    "refusal": state.accumulated_refusal,
                },
            },
        )

    if state.text_message_opened:
        # 构建完整的 message item
        complete_message = _build_complete_message_item(state)
        output_items.append(complete_message)

        # response.output_item.done
        yield _emit_sse_event(
            Constants.EVENT_OUTPUT_ITEM_DONE,
            {
                "output_index": state.text_output_index,
                "item": complete_message,
            },
        )

    # 关闭 reasoning 块（o 系列推理模型的推理内容）
    if state.reasoning_opened:
        # response.reasoning.done
        yield _emit_sse_event(
            Constants.EVENT_REASONING_DONE,
            {
                "output_index": state.reasoning_output_index,
                "text": state.accumulated_reasoning,
            },
        )

        # 构建完整的 reasoning output item（复用首次生成的 ID）
        complete_reasoning = {
            "type": Constants.REASONING,
            "id": state.reasoning_item_id,
            "summary": [
                {
                    "type": "summary_text",
                    "text": state.accumulated_reasoning,
                }
            ],
        }
        output_items.append(complete_reasoning)

        # response.output_item.done
        yield _emit_sse_event(
            Constants.EVENT_OUTPUT_ITEM_DONE,
            {
                "output_index": state.reasoning_output_index,
                "item": complete_reasoning,
            },
        )

    # 关闭工具块（强制启动未就绪的块）
    # 按 output_index 排序，确保顺序正确
    sorted_blocks = sorted(
        state.tool_blocks.items(),
        key=lambda x: x[1].output_index if x[1].started else 999999,
    )

    for tc_index, block in sorted_blocks:
        # 强制启动未就绪的块
        if not block.started:
            block.started = True
            block.output_index = state.next_output_index
            state.next_output_index += 1

            # 使用回退值
            if not block.call_id:
                block.call_id = f"tool_call_{tc_index}"
            if not block.name:
                block.name = "unknown_tool"
            # 生成并保存 item ID
            block.item_id = f"fc_{uuid.uuid4().hex[:24]}"

            # 发送 output_item.added
            fc_item = {
                "type": Constants.FUNCTION_CALL,
                "id": block.item_id,
                "call_id": block.call_id,
                "name": block.name,
                "arguments": "",
                "status": Constants.STATUS_IN_PROGRESS,
            }
            yield _emit_sse_event(
                Constants.EVENT_OUTPUT_ITEM_ADDED,
                {
                    "output_index": block.output_index,
                    "item": fc_item,
                },
            )

            # 发送暂存的 arguments
            if block.accumulated_args:
                yield _emit_sse_event(
                    Constants.EVENT_FUNCTION_CALL_ARGS_DELTA,
                    {
                        "output_index": block.output_index,
                        "call_id": block.call_id,
                        "delta": block.accumulated_args,
                    },
                )

        # response.function_call_arguments.done
        yield _emit_sse_event(
            Constants.EVENT_FUNCTION_CALL_ARGS_DONE,
            {
                "output_index": block.output_index,
                "call_id": block.call_id,
                "arguments": block.accumulated_args,
            },
        )

        # 构建完整的 function_call item
        complete_fc = _build_complete_function_call_item(block)
        output_items.append(complete_fc)

        # response.output_item.done
        yield _emit_sse_event(
            Constants.EVENT_OUTPUT_ITEM_DONE,
            {
                "output_index": block.output_index,
                "item": complete_fc,
            },
        )

    # response.completed
    status, incomplete_details = _map_finish_reason_to_status(finish_reason)
    usage = _convert_usage(state.final_usage)

    # 按 output_index 排序输出
    output_items.sort(key=lambda x: _get_item_output_index(x, state))

    response_obj = _build_response_object_dict(
        response_id=state.response_id,
        model=state.model,
        status=status,
        output=output_items,
        usage=usage,
        incomplete_details=incomplete_details,
    )

    # response.completed — response 嵌套在 "response" 字段中
    yield _emit_sse_event(
        Constants.EVENT_RESPONSE_COMPLETED,
        {"response": response_obj},
    )


# ========== 辅助构建函数 ==========


def _generate_response_id() -> str:
    """生成 Responses API 格式的响应 ID"""
    return f"{Constants.RESPONSE_ID_PREFIX}{uuid.uuid4().hex[:24]}"


def _build_response_object_dict(
    response_id: str,
    model: str,
    status: str,
    output: list,
    usage: Optional[dict] = None,
    incomplete_details: Optional[dict] = None,
) -> dict:
    """构建完整的 Responses API response 对象

    Args:
        response_id: 响应 ID
        model: 模型名称
        status: 响应状态
        output: 输出项列表
        usage: 使用量信息
        incomplete_details: 不完整详情

    Returns:
        完整的 response 对象字典
    """
    response = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": output,
        "usage": usage or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }

    if incomplete_details:
        response["incomplete_details"] = incomplete_details

    return response


def _build_complete_message_item(state: StreamState) -> dict:
    """构建完整的 message output item

    复用 state 中首次生成的 message ID，确保与 output_item.added 事件中的 ID 一致。
    支持文本内容和 refusal 内容块的组合。

    Args:
        state: 流式状态

    Returns:
        完整的 message item 字典
    """
    content = []

    # 文本内容块
    if state.text_content_part_opened:
        content.append({
            "type": Constants.OUTPUT_TEXT,
            "text": state.accumulated_text,
            "annotations": [],
        })

    # refusal 内容块
    if state.refusal_opened:
        content.append({
            "type": Constants.REFUSAL,
            "refusal": state.accumulated_refusal,
        })

    # 兜底：如果既没有文本也没有 refusal，使用空文本
    if not content:
        content.append({
            "type": Constants.OUTPUT_TEXT,
            "text": state.accumulated_text,
            "annotations": [],
        })

    return {
        "type": "message",
        "id": state.text_message_id,
        "status": Constants.STATUS_COMPLETED,
        "role": Constants.ROLE_ASSISTANT,
        "content": content,
    }


def _build_complete_function_call_item(block: ToolBlockState) -> dict:
    """构建完整的 function_call output item

    复用 block 中首次生成的 item ID，确保与 output_item.added 事件中的 ID 一致。

    Args:
        block: 工具块状态

    Returns:
        完整的 function_call item 字典
    """
    return {
        "type": Constants.FUNCTION_CALL,
        "id": block.item_id,
        "call_id": block.call_id,
        "name": block.name,
        "arguments": block.accumulated_args,
        "status": Constants.STATUS_COMPLETED,
    }


def _build_reasoning_output_item(reasoning_text: str) -> dict:
    """构建 reasoning output item（非流式）

    当 Chat Completions 响应中包含 reasoning 字段时（o 系列推理模型），
    将其转换为 Responses API 的 reasoning output item。

    Args:
        reasoning_text: 推理内容文本

    Returns:
        Responses API 格式的 reasoning output item
    """
    return {
        "type": Constants.REASONING,
        "id": f"rs_{uuid.uuid4().hex[:24]}",
        "summary": [
            {
                "type": "summary_text",
                "text": reasoning_text,
            }
        ],
    }


def _build_message_output_item_with_refusal(content: str, refusal: str) -> dict:
    """构建包含文本和/或 refusal 的 message output item（非流式）

    将 content 和 refusal 合并到同一个 message 的 content 数组中，
    符合 Responses API 规范。

    Args:
        content: 文本内容（可为 None 或空字符串）
        refusal: refusal 文本（可为 None 或空字符串）

    Returns:
        Responses API 格式的 message output item
    """
    content_blocks = []

    # 文本内容块
    if content:
        content_blocks.append({
            "type": Constants.OUTPUT_TEXT,
            "text": content,
            "annotations": [],
        })

    # refusal 内容块
    if refusal:
        content_blocks.append({
            "type": Constants.REFUSAL,
            "refusal": refusal,
        })

    # 兜底：如果都为空，使用空文本内容块
    if not content_blocks:
        content_blocks.append({
            "type": Constants.OUTPUT_TEXT,
            "text": "",
            "annotations": [],
        })

    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "status": Constants.STATUS_COMPLETED,
        "role": Constants.ROLE_ASSISTANT,
        "content": content_blocks,
    }


def _build_message_output_item(content: str) -> dict:
    """构建文本消息 output item（非流式）

    Args:
        content: 文本内容

    Returns:
        Responses API 格式的 message output item
    """
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "status": Constants.STATUS_COMPLETED,
        "role": Constants.ROLE_ASSISTANT,
        "content": [
            {
                "type": Constants.OUTPUT_TEXT,
                "text": content,
                "annotations": [],
            }
        ],
    }


def _build_refusal_output_item(refusal: str) -> dict:
    """构建 refusal 消息 output item（非流式）

    当模型拒绝响应时，Chat Completions 返回 message.refusal 字段。
    转换为 Responses API 的 refusal 类型内容块。

    Args:
        refusal: 拒绝原因文本

    Returns:
        Responses API 格式的包含 refusal 内容块的 message output item
    """
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "status": Constants.STATUS_COMPLETED,
        "role": Constants.ROLE_ASSISTANT,
        "content": [
            {
                "type": Constants.REFUSAL,
                "refusal": refusal,
            }
        ],
    }


def _convert_usage(usage: Optional[dict]) -> dict:
    """转换 usage 字段

    Chat Completions: {prompt_tokens, completion_tokens, total_tokens, prompt_tokens_details, ...}
    Responses API: {input_tokens, output_tokens, total_tokens, input_tokens_details, output_tokens_details}

    同时处理缓存 token 信息：
    - 标准 OpenAI 格式: prompt_tokens_details.cached_tokens
    - 兼容服务器直传: cache_read_input_tokens, cache_creation_input_tokens

    Args:
        usage: Chat Completions 格式的 usage

    Returns:
        Responses API 格式的 usage
    """
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", input_tokens + output_tokens)

    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }

    # 提取缓存 token 信息
    cached_tokens = _extract_cached_tokens(usage)
    if cached_tokens is not None:
        result["input_tokens_details"] = {"cached_tokens": cached_tokens}

    # 提取输出 token 详情（如推理 token）
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        reasoning_tokens = completion_details.get("reasoning_tokens")
        if reasoning_tokens is not None:
            result["output_tokens_details"] = {"reasoning_tokens": reasoning_tokens}

    return result


def _extract_cached_tokens(usage: dict) -> Optional[int]:
    """从 usage 中提取缓存 token 数量

    按优先级尝试以下来源：
    1. OpenAI 标准嵌套格式: prompt_tokens_details.cached_tokens
    2. 兼容服务器直传格式: cache_read_input_tokens

    Args:
        usage: Chat Completions 格式的 usage

    Returns:
        缓存 token 数量，无缓存信息时返回 None
    """
    # 优先级 1: OpenAI 标准嵌套格式
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached = prompt_details.get("cached_tokens")
        if cached is not None:
            return cached

    # 优先级 2: 兼容服务器直传格式（某些 OpenAI 兼容 API 使用 Anthropic 风格字段）
    cache_read = usage.get("cache_read_input_tokens")
    if cache_read is not None:
        return cache_read

    return None


def _map_finish_reason_to_status(finish_reason: str) -> tuple[str, Optional[dict]]:
    """将 Chat Completions 的 finish_reason 映射到 Responses API status

    Args:
        finish_reason: Chat Completions 的 finish_reason

    Returns:
        (status, incomplete_details) 元组
    """
    if finish_reason == Constants.FINISH_LENGTH:
        return (
            Constants.STATUS_INCOMPLETE,
            {"reason": "max_output_tokens"},
        )

    if finish_reason == Constants.FINISH_CONTENT_FILTER:
        return (
            Constants.STATUS_INCOMPLETE,
            {"reason": "content_filter"},
        )

    # stop, tool_calls 等都映射为 completed
    return (Constants.STATUS_COMPLETED, None)


def _get_item_output_index(item: dict, state: StreamState) -> int:
    """获取 output item 在流中的输出索引

    用于排序 output_items。

    Args:
        item: output item 字典
        state: 流式状态

    Returns:
        输出索引
    """
    item_type = item.get("type")

    if item_type == "message":
        return state.text_output_index

    if item_type == Constants.REASONING:
        return state.reasoning_output_index

    if item_type == Constants.FUNCTION_CALL:
        # 通过 call_id 查找对应的 block
        call_id = item.get("call_id", "")
        for tc_index, block in state.tool_blocks.items():
            if block.call_id == call_id:
                return block.output_index

    return 999999
