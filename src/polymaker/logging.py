"""structlog configuration: human console in dev, JSON to file in prod."""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

# HTTP 流量日志(httpx 每个请求一行)的轮转上限:单市场安静运行时约 6000 行/天
# (~1MB),每多一个市场再加约 1440 行/天。20MB × 4 份足够覆盖数周长跑。
_HTTP_LOG_MAX_BYTES = 20 * 1024 * 1024
_HTTP_LOG_BACKUPS = 3


class _UTCFormatter(logging.Formatter):
    """按 UTC 打印,与 structlog 的 TimeStamper(fmt="iso", utc=True) 对齐。"""

    converter = time.gmtime


class _HttpTrafficFilter(logging.Filter):
    """把 httpx 的「每次请求一行」日志从某个 handler 里摘出去。

    这些行(由 httpx 自己的 logger 打出)会把策略日志完全淹没,但 4xx/5xx 又
    常常是唯一的排障线索 —— 例如 `POST /auth/api-key 400` 是判断「create 失败、
    derive 兜底成功」的依据。所以分两档:

      keep_failures=True  -> 只放行 4xx/5xx(控制台:留关键证据,挡掉 2xx 洪流)
      keep_failures=False -> httpx 的 HTTP Request 行全挡(归档:全量已在 http.log)

    其它 logger(含 py_clob_client_v2 自己的报错行)一律放行。
    """

    def __init__(self, *, keep_failures: bool) -> None:
        super().__init__()
        self._keep_failures = keep_failures

    @staticmethod
    def _status_code(msg: str) -> int | None:
        # 形如: HTTP Request: GET https://host/path "HTTP/1.1 200 OK"
        parts = msg.split('"')
        if len(parts) < 2:
            return None
        fields = parts[-2].split()
        if len(fields) < 2:
            return None
        try:
            return int(fields[1])
        except ValueError:
            return None

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if record.name != "httpx" or "HTTP Request:" not in msg:
            return True
        code = self._status_code(msg)
        if code is None:  # 解析不出状态码就放行 —— 宁可多打,不可漏掉
            return True
        if code >= 400:
            return self._keep_failures
        return False  # 2xx/3xx 一律挡掉


def configure(
    *,
    level: str = "INFO",
    json_file: Path | None = None,
    http_log: Path | None = None,
    console: bool = True,
) -> None:
    """Set up structlog + stdlib logging once at process start.

    `http_log` 传了就把 httpx 的逐请求日志分流到该文件(带轮转),控制台只保留
    其中的 4xx/5xx;不传则保持原样(httpx 日志照旧进控制台)。
    """
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
                ]
            )
        )
        if http_log is not None:
            # HTTP 行的全量已分流到 http.log;控制台只留 4xx/5xx 这类关键证据
            ch.addFilter(_HttpTrafficFilter(keep_failures=True))
        root.addHandler(ch)

    if json_file is not None:
        json_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(json_file)
        fh.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ]
            )
        )
        if http_log is not None:
            # 归档里也不要混 httpx 的流量行 —— 全量在 http.log,这里只留策略事件
            fh.addFilter(_HttpTrafficFilter(keep_failures=False))
        root.addHandler(fh)

    if http_log is not None:
        http_log.parent.mkdir(parents=True, exist_ok=True)
        httpx_logger = logging.getLogger("httpx")
        for h in list(httpx_logger.handlers):  # 幂等:重复 configure 不叠加 handler
            httpx_logger.removeHandler(h)
            h.close()
        hh = RotatingFileHandler(http_log, maxBytes=_HTTP_LOG_MAX_BYTES,
                                 backupCount=_HTTP_LOG_BACKUPS, encoding="utf-8")
        hh.setFormatter(_UTCFormatter("%(asctime)sZ %(message)s",
                                      datefmt="%Y-%m-%dT%H:%M:%S"))
        httpx_logger.addHandler(hh)
        # propagate 保持 True:控制台/JSON 两路由 _HttpTrafficFilter 摘掉 HTTP 行,
        # 所以不必切断传播 —— 万一将来加了新 handler,httpx 也不会被漏掉。


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
