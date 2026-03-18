import os
import sys
from src.core.constants import Constants


class Config:
    """代理服务配置类

    从环境变量读取配置，支持自定义请求头。
    """

    def __init__(self):
        self.openai_api_key = os.environ.get("OPENAI_API_KEY")
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables")

        # 代理自身的 API 密钥验证
        # 使用 PROXY_API_KEY 避免与客户端工具的环境变量冲突
        self.client_api_key = os.environ.get("PROXY_API_KEY")
        if not self.client_api_key:
            print("Warning: PROXY_API_KEY not set. Client API key validation will be disabled.")

        self.openai_base_url = os.environ.get(
            "OPENAI_BASE_URL", Constants.DEFAULT_OPENAI_BASE_URL
        )
        self.host = os.environ.get("HOST", Constants.DEFAULT_HOST)
        self.port = int(os.environ.get("PORT", str(Constants.DEFAULT_PORT)))
        self.log_level = os.environ.get("LOG_LEVEL", Constants.DEFAULT_LOG_LEVEL)

        # 连接超时设置
        self.request_timeout = int(
            os.environ.get("REQUEST_TIMEOUT", str(Constants.DEFAULT_REQUEST_TIMEOUT))
        )
        self.read_timeout = int(
            os.environ.get("READ_TIMEOUT", str(Constants.DEFAULT_READ_TIMEOUT))
        )
        self.max_retries = int(
            os.environ.get("MAX_RETRIES", str(Constants.DEFAULT_MAX_RETRIES))
        )

    def validate_client_api_key(self, client_api_key):
        """验证客户端提供的 API 密钥

        如果环境变量中未设置 PROXY_API_KEY，则跳过验证。
        """
        # 未设置 PROXY_API_KEY 时跳过验证
        if not self.client_api_key:
            return True

        # 检查客户端密钥是否匹配
        return client_api_key == self.client_api_key

    def get_custom_headers(self):
        """从环境变量中获取自定义请求头

        查找所有以 CUSTOM_HEADER_ 开头的环境变量，
        将其转换为 HTTP 请求头格式（下划线转为连字符）。
        """
        custom_headers = {}

        # 获取所有环境变量
        env_vars = dict(os.environ)

        # 查找 CUSTOM_HEADER_* 环境变量
        for env_key, env_value in env_vars.items():
            if env_key.startswith('CUSTOM_HEADER_'):
                # 移除 'CUSTOM_HEADER_' 前缀并转换为请求头格式
                header_name = env_key[14:]  # 移除 'CUSTOM_HEADER_' 前缀

                if header_name:  # 确保不为空
                    # 下划线转连字符，符合 HTTP 请求头规范
                    header_name = header_name.replace('_', '-')
                    custom_headers[header_name] = env_value

        return custom_headers


# 模块加载时实例化配置
try:
    config = Config()
    print(f" Configuration loaded: API_KEY={'*' * 20}..., BASE_URL='{config.openai_base_url}'")
except Exception as e:
    print(f"Configuration Error: {e}")
    sys.exit(1)
