"""记忆系统日志 - VectorStore 和整理器"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOGS_DIR = "logs/memory"
os.makedirs(LOGS_DIR, exist_ok=True)

memory_logger = logging.getLogger("memory")
memory_logger.setLevel(logging.INFO)

if not memory_logger.handlers:
    handler = RotatingFileHandler(
        f"{LOGS_DIR}/memory.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    memory_logger.addHandler(handler)
    memory_logger.propagate = False

