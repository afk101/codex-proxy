import logging
from src.core.config import config

# 解析日志级别 - 提取第一个单词以处理注释
log_level = config.log_level.split()[0].upper()

# 验证日志级别，无效则使用默认值
valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
if log_level not in valid_levels:
    log_level = 'INFO'

# 日志配置
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# 降低 uvicorn 日志级别，避免过多输出
for uvicorn_logger in ["uvicorn", "uvicorn.access", "uvicorn.error"]:
    logging.getLogger(uvicorn_logger).setLevel(logging.WARNING)
