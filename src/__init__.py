"""Codex Proxy

将 OpenAI Responses API 请求转换为 Chat Completions API 格式的代理服务器。
"""

from dotenv import load_dotenv

# 从 .env 文件加载环境变量
load_dotenv()
__version__ = "1.0.0"
__author__ = "Codex Proxy"
