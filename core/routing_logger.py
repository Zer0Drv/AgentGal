"""路由决策和性能日志记录"""

import os
import logging
from logging.handlers import RotatingFileHandler


# 创建 logs/routing 目录
LOGS_DIR = "logs/routing"
os.makedirs(LOGS_DIR, exist_ok=True)

# 创建 logger
routing_logger = logging.getLogger("routing")
routing_logger.setLevel(logging.INFO)

# 避免重复添加 handler
if not routing_logger.handlers:
    # 创建文件 handler，写入 JSONL 格式
    handler = RotatingFileHandler(
        f"{LOGS_DIR}/routing.log",
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding="utf-8"
    )
    handler.setLevel(logging.INFO)

    formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)

    routing_logger.addHandler(handler)
    # 不传播到 root logger
    routing_logger.propagate = False

