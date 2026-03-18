# 常量定义模块
# 集中管理所有常量，避免魔法字符串


class Constants:
    """所有常量的集中定义"""

    # ========== 角色常量 ==========
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_SYSTEM = "system"
    ROLE_TOOL = "tool"

    # ========== Responses API input 类型 ==========
    INPUT_TEXT = "input_text"
    OUTPUT_TEXT = "output_text"
    INPUT_IMAGE = "input_image"
    FUNCTION_CALL = "function_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"
    REFUSAL = "refusal"  # 模型拒绝响应时的内容块类型
    REASONING = "reasoning"  # o 系列推理模型的推理内容类型

    # ========== Chat Completions 常量 ==========
    TOOL_FUNCTION = "function"
    FINISH_STOP = "stop"
    FINISH_LENGTH = "length"
    FINISH_TOOL_CALLS = "tool_calls"
    FINISH_CONTENT_FILTER = "content_filter"

    # ========== Responses API 状态 ==========
    STATUS_COMPLETED = "completed"
    STATUS_INCOMPLETE = "incomplete"
    STATUS_IN_PROGRESS = "in_progress"

    # ========== Responses API SSE 事件类型 ==========
    EVENT_RESPONSE_CREATED = "response.created"
    EVENT_RESPONSE_IN_PROGRESS = "response.in_progress"
    EVENT_RESPONSE_COMPLETED = "response.completed"
    EVENT_OUTPUT_ITEM_ADDED = "response.output_item.added"
    EVENT_OUTPUT_ITEM_DONE = "response.output_item.done"
    EVENT_CONTENT_PART_ADDED = "response.content_part.added"
    EVENT_CONTENT_PART_DONE = "response.content_part.done"
    EVENT_OUTPUT_TEXT_DELTA = "response.output_text.delta"
    EVENT_OUTPUT_TEXT_DONE = "response.output_text.done"
    EVENT_FUNCTION_CALL_ARGS_DELTA = "response.function_call_arguments.delta"
    EVENT_FUNCTION_CALL_ARGS_DONE = "response.function_call_arguments.done"
    EVENT_REFUSAL_DELTA = "response.refusal.delta"
    EVENT_REFUSAL_DONE = "response.refusal.done"
    # reasoning 相关事件（o 系列推理模型返回 reasoning 内容时使用）
    EVENT_REASONING_DELTA = "response.reasoning.delta"
    EVENT_REASONING_DONE = "response.reasoning.done"

    # ========== 默认配置值 ==========
    DEFAULT_PORT = 9002
    DEFAULT_HOST = "0.0.0.0"
    DEFAULT_LOG_LEVEL = "INFO"
    DEFAULT_REQUEST_TIMEOUT = 90
    DEFAULT_READ_TIMEOUT = 300
    DEFAULT_MAX_RETRIES = 2
    DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

    # ========== 响应 ID 前缀 ==========
    RESPONSE_ID_PREFIX = "resp_"

    # ========== User-Agent ==========
    USER_AGENT = "codex-proxy/1.0.0"
