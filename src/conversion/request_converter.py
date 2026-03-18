"""请求转换器

将 OpenAI Responses API 请求转换为 Chat Completions API 请求格式。
"""

import copy
import logging
from typing import Any, Optional
from src.models.responses import ResponsesRequest
from src.core.constants import Constants

logger = logging.getLogger(__name__)


def convert_responses_to_chat_completion(request: ResponsesRequest) -> dict:
    """将 Responses API 请求转换为 Chat Completions API 请求

    Args:
        request: Responses API 请求对象

    Returns:
        Chat Completions API 请求字典
    """
    result = {"model": request.model}

    messages = []

    # 转换 instructions 为 system 消息
    system_message = _convert_instructions_to_system_message(request.instructions)
    if system_message:
        messages.append(system_message)

    # 转换 input 为 messages
    if isinstance(request.input, str):
        # 简单字符串输入
        messages.append({
            "role": Constants.ROLE_USER,
            "content": request.input,
        })
    elif isinstance(request.input, list):
        # input items 数组
        converted_messages = _convert_input_items_to_messages(request.input)
        messages.extend(converted_messages)

    result["messages"] = messages

    # 转换 max_output_tokens
    if request.max_output_tokens is not None:
        if _is_o_series_model(request.model):
            # o 系列模型使用 max_completion_tokens
            result["max_completion_tokens"] = request.max_output_tokens
        else:
            result["max_tokens"] = request.max_output_tokens

    # 直接透传的参数
    if request.temperature is not None:
        result["temperature"] = request.temperature
    if request.top_p is not None:
        result["top_p"] = request.top_p
    if request.stream is not None:
        result["stream"] = request.stream

    # 转换 tools
    if request.tools:
        converted_tools = _convert_tools(request.tools)
        if converted_tools:
            result["tools"] = converted_tools

    # 转换 tool_choice
    if request.tool_choice is not None:
        result["tool_choice"] = _convert_tool_choice(request.tool_choice)

    # 转换 reasoning 配置（Responses API → Chat Completions API）
    # Responses API: {"reasoning": {"effort": "high"}}
    # Chat Completions API: {"reasoning_effort": "high"}
    if request.reasoning is not None:
        reasoning_effort = _extract_reasoning_effort(request.reasoning)
        if reasoning_effort:
            result["reasoning_effort"] = reasoning_effort

    # 透传 parallel_tool_calls（Chat Completions API 原生支持）
    if request.parallel_tool_calls is not None:
        result["parallel_tool_calls"] = request.parallel_tool_calls

    return result


def _convert_instructions_to_system_message(instructions: Optional[str]) -> Optional[dict]:
    """将 instructions 转换为 system 消息

    Args:
        instructions: Responses API 的 instructions 字段

    Returns:
        system 消息字典，如果 instructions 为空则返回 None
    """
    if not instructions:
        return None
    return {
        "role": Constants.ROLE_SYSTEM,
        "content": instructions,
    }


def _convert_input_items_to_messages(items: list[Any]) -> list[dict]:
    """将 Responses API input items 数组转换为 Chat Completions messages

    处理逻辑：
    - user/assistant 角色的消息直接转换
    - function_call items 合并到 assistant 消息的 tool_calls 数组中
    - function_call_output items 转为 tool 角色消息
    - 相邻的 function_call items 合并到同一个 assistant 消息

    Args:
        items: Responses API input items 数组

    Returns:
        Chat Completions messages 列表
    """
    messages = []
    i = 0

    while i < len(items):
        item = items[i]

        # 处理字典类型的 item
        if not isinstance(item, dict):
            i += 1
            continue

        item_type = item.get("type")
        item_role = item.get("role")

        # 处理 type="message" 格式的 input item（Codex CLI 使用这种格式）
        # 需要提取 role 来判断消息类型
        if item_type == "message" and item_role:
            content = item.get("content")
            # developer 角色映射为 system
            mapped_role = Constants.ROLE_SYSTEM if item_role == "developer" else item_role
            if isinstance(content, str):
                messages.append({
                    "role": mapped_role,
                    "content": content,
                })
            elif isinstance(content, list):
                if mapped_role == Constants.ROLE_USER:
                    converted_content = _convert_user_content_blocks(content)
                    messages.append({
                        "role": mapped_role,
                        "content": converted_content,
                    })
                elif mapped_role == Constants.ROLE_ASSISTANT:
                    converted_content = _convert_assistant_content_blocks(content)
                    messages.append({
                        "role": mapped_role,
                        "content": converted_content,
                    })
                else:
                    # system/developer 等角色，提取文本
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text", "")
                            if text:
                                text_parts.append(text)
                    messages.append({
                        "role": mapped_role,
                        "content": "\n".join(text_parts) if text_parts else "",
                    })
            else:
                messages.append({
                    "role": mapped_role,
                    "content": str(content) if content is not None else "",
                })
            i += 1
            continue

        if item_type == Constants.FUNCTION_CALL:
            # function_call item - 收集连续的 function_call 并合并
            tool_calls = []
            # 检查前一个消息是否是 assistant（可以合并 tool_calls）
            prev_assistant = None
            if messages and messages[-1].get("role") == Constants.ROLE_ASSISTANT:
                prev_assistant = messages[-1]

            # 收集所有连续的 function_call items
            while i < len(items) and isinstance(items[i], dict) and items[i].get("type") == Constants.FUNCTION_CALL:
                tool_call = _convert_function_call_to_tool_call(items[i])
                tool_calls.append(tool_call)
                i += 1

            if prev_assistant is not None:
                # 合并到前一个 assistant 消息
                if "tool_calls" not in prev_assistant:
                    prev_assistant["tool_calls"] = []
                prev_assistant["tool_calls"].extend(tool_calls)
                # 如果 assistant 消息没有文本内容，设置 content 为 None
                if not prev_assistant.get("content"):
                    prev_assistant["content"] = None
            else:
                # 创建新的 assistant 消息
                messages.append({
                    "role": Constants.ROLE_ASSISTANT,
                    "content": None,
                    "tool_calls": tool_calls,
                })
            continue

        elif item_type == Constants.FUNCTION_CALL_OUTPUT:
            # function_call_output item - 转为 tool 角色消息
            messages.append(_convert_function_call_output_to_message(item))
            i += 1
            continue

        elif item_type == Constants.REASONING:
            # reasoning item — 多轮对话中上一轮的 reasoning 输出被回传
            # Chat Completions API 不需要此信息，静默跳过
            i += 1
            continue

        elif item_role == Constants.ROLE_USER:
            # 用户消息
            content = item.get("content")
            if isinstance(content, str):
                messages.append({
                    "role": Constants.ROLE_USER,
                    "content": content,
                })
            elif isinstance(content, list):
                converted_content = _convert_user_content_blocks(content)
                messages.append({
                    "role": Constants.ROLE_USER,
                    "content": converted_content,
                })
            else:
                # 兼容: content 为空或其他格式
                messages.append({
                    "role": Constants.ROLE_USER,
                    "content": str(content) if content is not None else "",
                })
            i += 1
            continue

        elif item_role == Constants.ROLE_ASSISTANT:
            # 助手消息
            content = item.get("content")
            if isinstance(content, str):
                messages.append({
                    "role": Constants.ROLE_ASSISTANT,
                    "content": content,
                })
            elif isinstance(content, list):
                converted_content = _convert_assistant_content_blocks(content)
                messages.append({
                    "role": Constants.ROLE_ASSISTANT,
                    "content": converted_content,
                })
            else:
                messages.append({
                    "role": Constants.ROLE_ASSISTANT,
                    "content": str(content) if content is not None else "",
                })
            i += 1
            continue

        elif item_role == Constants.ROLE_SYSTEM or item_role == "developer":
            # system 或 developer 消息（developer 映射为 system）
            mapped_role = Constants.ROLE_SYSTEM
            content = item.get("content")
            if isinstance(content, str):
                messages.append({
                    "role": Constants.ROLE_SYSTEM,
                    "content": content,
                })
            elif isinstance(content, list):
                # 提取文本内容
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text", "")
                        if text:
                            text_parts.append(text)
                messages.append({
                    "role": Constants.ROLE_SYSTEM,
                    "content": "\n".join(text_parts) if text_parts else "",
                })
            i += 1
            continue

        else:
            # 未知类型，跳过并记录警告
            logger.warning(f"跳过未知的 input item 类型: type={item_type}, role={item_role}")
            i += 1
            continue

    return messages


def _convert_user_content_blocks(content: list) -> Any:
    """转换用户消息的内容块

    处理 input_text 和 input_image 类型。
    如果只有单个文本块，简化为字符串。

    Args:
        content: 内容块列表

    Returns:
        转换后的内容（字符串或多模态内容数组）
    """
    # 检查是否只有单个文本块
    if len(content) == 1 and isinstance(content[0], dict):
        block = content[0]
        block_type = block.get("type", "")
        if block_type == Constants.INPUT_TEXT:
            return block.get("text", "")

    # 多模态内容 - 转换为 Chat Completions 格式
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type", "")

        if block_type == Constants.INPUT_TEXT:
            parts.append({
                "type": "text",
                "text": block.get("text", ""),
            })
        elif block_type == Constants.INPUT_IMAGE:
            # 图片内容
            image_url = block.get("image_url", "")
            if isinstance(image_url, str):
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": image_url},
                })
            elif isinstance(image_url, dict):
                parts.append({
                    "type": "image_url",
                    "image_url": image_url,
                })
        else:
            # 未知类型，尝试作为文本处理
            text = block.get("text", "")
            if text:
                parts.append({
                    "type": "text",
                    "text": text,
                })

    # 如果只有一个文本部分，简化为字符串
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]

    return parts


def _convert_assistant_content_blocks(content: list) -> Any:
    """转换助手消息的内容块

    处理 output_text 类型。
    如果只有单个文本块，简化为字符串。

    Args:
        content: 内容块列表

    Returns:
        转换后的内容（字符串或 None）
    """
    text_parts = []
    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type", "")

        if block_type == Constants.OUTPUT_TEXT:
            text = block.get("text", "")
            if text:
                text_parts.append(text)
        elif block_type == Constants.REFUSAL:
            # refusal 内容块 — 历史消息中可能包含 refusal，提取文本
            refusal_text = block.get("refusal", "")
            if refusal_text:
                text_parts.append(refusal_text)

    if not text_parts:
        return None

    # 合并所有文本
    return "\n".join(text_parts) if len(text_parts) > 1 else text_parts[0]


def _convert_function_call_to_tool_call(item: dict) -> dict:
    """将 function_call item 转换为 Chat Completions tool_call 对象

    Args:
        item: Responses API 的 function_call item

    Returns:
        Chat Completions 格式的 tool_call 字典
    """
    return {
        "id": item.get("call_id", ""),
        "type": Constants.TOOL_FUNCTION,
        "function": {
            "name": item.get("name", ""),
            "arguments": item.get("arguments", "{}"),
        },
    }


def _convert_function_call_output_to_message(item: dict) -> dict:
    """将 function_call_output item 转换为 tool 角色消息

    Args:
        item: Responses API 的 function_call_output item

    Returns:
        Chat Completions 格式的 tool 角色消息
    """
    return {
        "role": Constants.ROLE_TOOL,
        "tool_call_id": item.get("call_id", ""),
        "content": item.get("output", ""),
    }


def _convert_tools(tools: list[dict]) -> list[dict]:
    """转换工具定义格式

    Responses API: {type, name, description, parameters}
    Chat Completions: {type: "function", function: {name, description, parameters}}

    注意：跳过 name 为空或不存在的工具，
    以及非 function 类型的工具（如 computer_use_preview 等）。

    Args:
        tools: Responses API 格式的工具列表

    Returns:
        Chat Completions 格式的工具列表
    """
    converted = []
    for tool in tools:
        tool_type = tool.get("type", Constants.TOOL_FUNCTION)
        tool_name = tool.get("name", "")

        # 跳过非 function 类型的工具（如 code_interpreter, computer_use_preview 等）
        if tool_type != Constants.TOOL_FUNCTION:
            logger.warning(f"跳过非 function 类型的工具: type={tool_type}, name={tool_name}")
            continue

        # 跳过 name 为空的工具，上游 API 不接受
        if not tool_name:
            logger.warning(f"跳过 name 为空的工具: {tool}")
            continue

        # 清理 parameters schema 中不兼容的属性
        parameters = _clean_schema(tool.get("parameters", {}))

        converted.append({
            "type": Constants.TOOL_FUNCTION,
            "function": {
                "name": tool_name,
                "description": tool.get("description", ""),
                "parameters": parameters,
            },
        })

    return converted


def _convert_tool_choice(tool_choice: Any) -> Any:
    """转换 tool_choice 格式

    Responses API 支持:
    - "auto", "none", "required" → 直接透传
    - {type: "function", name: "xxx"} → {type: "function", function: {name: "xxx"}}

    Args:
        tool_choice: Responses API 的 tool_choice 值

    Returns:
        Chat Completions 格式的 tool_choice
    """
    if isinstance(tool_choice, str):
        # "auto", "none", "required" 直接透传
        return tool_choice

    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type")
        if tc_type == Constants.TOOL_FUNCTION:
            # 带具体函数名的 tool_choice
            return {
                "type": Constants.TOOL_FUNCTION,
                "function": {
                    "name": tool_choice.get("name", ""),
                },
            }

    # 其他情况直接透传
    return tool_choice


def _is_o_series_model(model: str) -> bool:
    """判断是否为 OpenAI o 系列推理模型

    o 系列模型（o1, o3, o4-mini 等）需要使用 max_completion_tokens
    而不是 max_tokens。

    Args:
        model: 模型名称

    Returns:
        是否为 o 系列模型
    """
    if len(model) <= 1:
        return False
    return model.startswith('o') and model[1].isdigit()


def _extract_reasoning_effort(reasoning: dict) -> Optional[str]:
    """从 Responses API 的 reasoning 配置中提取 reasoning_effort

    Responses API 格式: {"effort": "high" | "medium" | "low"}
    Chat Completions API 格式: reasoning_effort = "high" | "medium" | "low"

    Args:
        reasoning: Responses API 的 reasoning 配置字典

    Returns:
        reasoning_effort 值，无效时返回 None
    """
    if not isinstance(reasoning, dict):
        return None
    effort = reasoning.get("effort")
    if effort in ("high", "medium", "low"):
        return effort
    if effort is not None:
        logger.warning(f"未知的 reasoning effort 值: {effort}，已忽略")
    return None


def _clean_schema(schema: Any) -> Any:
    """递归清理 JSON Schema 中不被某些上游 API 支持的属性

    主要清理:
    - "format": "uri" — 某些 OpenAI 兼容 API 不支持此 format 值
    参考 cc-switch 的 clean_schema 实现。

    Args:
        schema: JSON Schema 字典（或其他类型，非字典时直接返回）

    Returns:
        清理后的 schema
    """
    if not isinstance(schema, dict):
        return schema

    # 深拷贝避免修改原始数据
    result = copy.deepcopy(schema)
    _clean_schema_in_place(result)
    return result


def _clean_schema_in_place(schema: dict) -> None:
    """原地递归清理 schema 中的不兼容属性

    Args:
        schema: 要清理的 JSON Schema 字典
    """
    # 移除不兼容的 format 值
    if schema.get("format") == "uri":
        del schema["format"]

    # 递归处理 properties
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for prop_schema in properties.values():
            if isinstance(prop_schema, dict):
                _clean_schema_in_place(prop_schema)

    # 递归处理 items（数组类型）
    items = schema.get("items")
    if isinstance(items, dict):
        _clean_schema_in_place(items)

    # 递归处理 additionalProperties
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        _clean_schema_in_place(additional)

    # 递归处理 anyOf / oneOf / allOf
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    _clean_schema_in_place(variant)
