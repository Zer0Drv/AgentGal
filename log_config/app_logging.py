"""Application logging helpers.

Business events go through stdlib logging. When Logfire is configured, these
loggers are forwarded to Logfire instead of writing local rotating log files.
"""

from __future__ import annotations

import logging
from typing import Any

APP_LOGGER_NAMES = ("agentgal.routing", "agentgal.memory")
_LOGFIRE_HANDLER_MARKER = "_agentgal_logfire_handler"


def get_app_logger(component: str) -> logging.Logger:
    """Return a namespaced logger for application-level events."""
    logger = logging.getLogger(f"agentgal.{component}")
    logger.setLevel(logging.INFO)
    return logger


def attach_logfire_handlers(logfire_module: Any) -> None:
    """Forward application loggers to Logfire when its logging handler exists."""
    handler_cls = getattr(logfire_module, "LogfireLoggingHandler", None)
    if handler_cls is None:
        return

    for logger_name in APP_LOGGER_NAMES:
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
