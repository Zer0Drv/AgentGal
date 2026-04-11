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
_APP_LOGGER_NAMES = ("agentgal.routing", "agentgal.memory")
_LOGFIRE_HANDLER_MARKER = "_agentgal_logfire_handler"


def attach_logfire_handlers(logfire_module: Any) -> None:
    """Forward application loggers to Logfire when its logging handler exists."""
    handler_cls = getattr(logfire_module, "LogfireLoggingHandler", None)
    if handler_cls is None:
        return

    for logger_name in _APP_LOGGER_NAMES:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        if any(getattr(handler, _LOGFIRE_HANDLER_MARKER, False) for handler in logger.handlers):
            continue

        try:
            handler = handler_cls(level=logging.INFO)
        except Exception:  # noqa: BLE001
            continue

        setattr(handler, _LOGFIRE_HANDLER_MARKER, True)
        logger.addHandler(handler)
        logger.propagate = False


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
            attach_logfire_handlers(logfire)
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
