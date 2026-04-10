"""Logfire 初始化与轻量辅助。"""

from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from threading import Lock
from typing import Any

_logfire = None
_configured = False
_lock = Lock()
_logger = logging.getLogger("logfire")


def setup_logfire() -> None:
    """按需初始化 Logfire。

    未安装、未配置或初始化失败时静默跳过，不影响主流程。
    """
    global _configured, _logfire

    if _configured:
        return

    with _lock:
        if _configured:
            return

        try:
            import logfire
        except ImportError:
            _configured = True
            return

        try:
            environment = os.getenv("LOGFIRE_ENVIRONMENT") or None
            logfire.configure(
                environment=environment,
            )
            logfire.instrument_pydantic_ai()
            _logfire = logfire
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Logfire 初始化失败，已跳过: %s", exc)
        finally:
            _configured = True


def logfire_span(name: str, **attributes: Any):
    """返回一个可选的 Logfire span 上下文。"""
    setup_logfire()
    if _logfire is None:
        return nullcontext()
    try:
        return _logfire.span(name, **attributes)
    except Exception:  # noqa: BLE001
        return nullcontext()
