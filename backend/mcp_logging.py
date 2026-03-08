from __future__ import annotations

import json
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, TypeVar

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_MAX_LOG_VALUE_CHARS = 1500

F = TypeVar("F", bound=Callable[..., Any])


def resolve_mcp_log_dir() -> Path:
    explicit = os.environ.get("WATERFREE_MCP_LOG_DIR")
    if explicit:
        return Path(explicit).expanduser()

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "WaterFree" / "logs" / "mcp"

    return Path.home() / ".waterfree" / "logs" / "mcp"


def configure_mcp_logger(server_name: str) -> tuple[logging.Logger, Path]:
    log_dir = resolve_mcp_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{server_name}.log"

    logger = logging.getLogger(f"waterfree.mcp.{server_name}")
    if logger.handlers:
        return logger, log_file

    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.info("MCP logging initialized: %s", log_file)
    return logger, log_file


def log_tool_call(logger: logging.Logger, tool_name: str, **kwargs: Any) -> None:
    logger.info("tool_call name=%s args=%s", tool_name, _compact_json(kwargs))


def log_tool_result(logger: logging.Logger, tool_name: str, result: Any) -> None:
    logger.info("tool_result name=%s result=%s", tool_name, _summarize_value(result))


def log_tool_error(logger: logging.Logger, tool_name: str, exc: Exception) -> None:
    logger.exception("tool_error name=%s error=%s", tool_name, exc)


def instrument_tool(logger: logging.Logger, tool_name: str, fn: F) -> F:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        log_tool_call(logger, tool_name, **kwargs)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            log_tool_error(logger, tool_name, exc)
            raise
        log_tool_result(logger, tool_name, result)
        return result

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__signature__ = inspect.signature(fn)
    return wrapper  # type: ignore[return-value]


def _compact_json(value: Any) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except TypeError:
        text = repr(value)
    return _truncate(text)


def _summarize_value(value: Any) -> str:
    if isinstance(value, str):
        return _truncate(value)
    return _compact_json(value)


def _truncate(text: str, limit: int = _MAX_LOG_VALUE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"
