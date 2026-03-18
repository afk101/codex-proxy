#!/usr/bin/env python3
"""启动 Codex Proxy 服务。"""

import sys
import os

# 将 src 添加到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from src.main import main

if __name__ == "__main__":
    main()
