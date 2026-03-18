from fastapi import FastAPI
from src.api.endpoints import router as api_router
import uvicorn
import sys
import logging
from src.core.config import config


class EndpointFilter(logging.Filter):
    """过滤特定端点的 uvicorn 访问日志"""

    def __init__(self, excluded_paths: list[str] = None):
        super().__init__()
        self.excluded_paths = excluded_paths or []

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for path in self.excluded_paths:
            if path in message:
                return False
        return True


app = FastAPI(title="Codex Proxy — Responses API to Chat Completions", version="1.0.0")

app.include_router(api_router)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print("Codex Proxy v1.0.0")
        print("  将 OpenAI Responses API 请求转换为 Chat Completions API 格式")
        print("")
        print("Usage: python src/main.py")
        print("")
        print("Required environment variables:")
        print("  OPENAI_API_KEY - 上游 OpenAI 兼容 API 的密钥")
        print("")
        print("Optional environment variables:")
        print("  PROXY_API_KEY     - 代理自身的 API 密钥验证")
        print(f"  OPENAI_BASE_URL - 上游 API 基础地址 (default: {config.openai_base_url})")
        print(f"  HOST - 服务器地址 (default: {config.host})")
        print(f"  PORT - 服务器端口 (default: {config.port})")
        print(f"  LOG_LEVEL - 日志级别 (default: {config.log_level})")
        print(f"  REQUEST_TIMEOUT - 请求超时（秒）(default: {config.request_timeout})")
        print(f"  READ_TIMEOUT - 流式读取超时（秒）(default: {config.read_timeout})")
        sys.exit(0)

    # 配置摘要
    print("🚀 Codex Proxy v1.0.0")
    print("   Responses API → Chat Completions API Proxy")
    print(f"✅ 配置加载成功")
    print(f"   OpenAI Base URL: {config.openai_base_url}")
    print(f"   Request Timeout: {config.request_timeout}s")
    print(f"   Read Timeout: {config.read_timeout}s")
    print(f"   Server: {config.host}:{config.port}")
    print(f"   Client API Key Validation: {'Enabled' if config.client_api_key else 'Disabled'}")
    print("")

    # 解析日志级别 - 提取第一个单词以处理注释
    log_level = config.log_level.split()[0].lower()

    # 验证日志级别
    valid_levels = ['debug', 'info', 'warning', 'error', 'critical']
    if log_level not in valid_levels:
        log_level = 'info'

    # 过滤 uvicorn 访问日志中的噪声端点
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(
        EndpointFilter(excluded_paths=["/health"])
    )

    # 启动服务
    uvicorn.run(
        "src.main:app",
        host=config.host,
        port=config.port,
        log_level=log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()
