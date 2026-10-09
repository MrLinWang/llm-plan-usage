"""运行日志:统一配置 CLI / TUI / Web 的日志落盘。

所有前端(show/tui/web)都在 ``cli.py`` 的 click 组回调里调用
:func:`setup_cli_logging`:日志同时写 stderr(保持原本的可见性)与一个
轮转文件,默认 ``./llm-usage.log``,可用环境变量 ``LLM_USAGE_LOG`` 覆盖
(Docker 镜像内置指向 /data 卷,容器重建日志不丢)。

两个关键点:

- 文件 handler 只挂在 root logger 上,uvicorn 等第三方 logger 通过
  propagate 汇入同一条流,避免每个 logger 各写一份文件;
- ``uvicorn.run`` 必须传 ``log_config=None``,否则 uvicorn 自己的
  dictConfig 会重置 logger 级别/处理器,把我们的配置顶掉。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_DIRNAME = "log"           # 日志统一放在 <配置目录>/log/ 下(git 忽略)
LOG_FILENAME = "llm-usage.log"

MAX_BYTES = 1_000_000   # 单文件 1MB,超出轮转
BACKUP_COUNT = 3        # 保留 llm-usage.log.1 ~ .3

# httpx 每次成功请求都打 INFO("HTTP Request: ..."),对 show/tui 是噪音、
# 还会快速写满日志文件;压到 WARNING 只保留真实问题
_HTTPX_LOGGER = "httpx"

# uvicorn 的日志 logger;web 模式下需要并入 root(见 setup_web_logging)
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class _MarkedStreamHandler(logging.StreamHandler):
    """stderr handler,类级标记用于幂等检查。"""

    _llm_usage_handler = True


class _MarkedRotatingFileHandler(RotatingFileHandler):
    """轮转文件 handler,类级标记用于幂等检查。"""

    _llm_usage_handler = True


def log_path() -> Path:
    """Return the resolved log file path.

    ``LLM_USAGE_LOG``(显式覆盖)优先;否则统一放在配置文件同目录的 ``log/``
    子目录下——``LLM_USAGE_CONFIG`` 指到 /data/config.toml 时即
    /data/log/llm-usage.log(仓库内已 gitignore ``log/``)。
    """
    env = os.environ.get("LLM_USAGE_LOG")
    if env:
        return Path(env)
    from llm_usage.config import config_path

    return config_path().parent / LOG_DIRNAME / LOG_FILENAME


def _build_root_handlers() -> list[logging.Handler]:
    """stderr + 轮转文件;文件创建失败(权限/路径)时降级为仅 stderr。"""
    fmt = logging.Formatter(LOG_FORMAT)
    stream = _MarkedStreamHandler()  # 默认 stderr:不污染 show --json 的 stdout
    stream.setFormatter(fmt)
    handlers: list[logging.Handler] = [stream]

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = _MarkedRotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        sys.stderr.write(f"llm-usage: 无法创建日志文件 {path}: {exc}\n")
        return handlers
    file_handler.setFormatter(fmt)
    handlers.append(file_handler)
    return handlers


def setup_cli_logging(level: int = logging.INFO) -> None:
    """配置 root logger(幂等):stderr + 轮转文件;httpx 压到 WARNING。

    重复调用不会叠加 handler;若外部替换过 root handler,则按本函数重装。
    """
    _pin_noisy_loggers()
    root = logging.getLogger()
    if any(getattr(handler, "_llm_usage_handler", False) for handler in root.handlers):
        return
    handlers = _build_root_handlers()
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def _pin_noisy_loggers() -> None:
    """第三方噪音压制(幂等;与 handler 安装解耦,重复调用也生效)。"""
    # 每次成功请求一行的 httpx INFO 日志:root 到 INFO 时刷屏并快速写满文件
    logging.getLogger(_HTTPX_LOGGER).setLevel(logging.WARNING)


def setup_web_logging(level: int = logging.INFO) -> None:
    """cli 日志配置 + 把 uvicorn 日志并入 root(同一文件 + stderr)。

    配合 ``uvicorn.run(..., log_config=None)`` 使用:此时 uvicorn 不再自行
    安装 handler,这里清掉可能存在的自带 handler 并打开 propagate,使启动/
    访问/错误日志都流经 root;否则 web 模式既不落盘也会在控制台双写。
    """
    setup_cli_logging(level)
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
