"""Tests for logging setup: httpx 流量日志的分流 + 控制台过滤。"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from polymaker.logging import _HttpTrafficFilter, configure


def _rec(name: str, msg: str) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, "p", 1, msg, None, None)


_OK = 'HTTP Request: GET https://data-api.polymarket.com/positions?user=0xabc "HTTP/1.1 200 OK"'
_BAD = 'HTTP Request: POST https://clob.polymarket.com/auth/api-key "HTTP/2 400 Bad Request"'
_OTHER = "[py_clob_client_v2] request error status=400 url=... body={...}"


def test_console_filter_keeps_only_failures():
    f = _HttpTrafficFilter(keep_failures=True)
    assert f.filter(_rec("httpx", _OK)) is False, "2xx 不该出现在控制台"
    assert f.filter(_rec("httpx", _BAD)) is True, "4xx/5xx 是关键证据,必须留"
    # 其它 logger / 非 HTTP 流量行一律放行
    assert f.filter(_rec("py_clob_client_v2", _OTHER)) is True
    assert f.filter(_rec("engine", "requote")) is True


def test_archive_filter_drops_all_http_traffic():
    f = _HttpTrafficFilter(keep_failures=False)
    assert f.filter(_rec("httpx", _OK)) is False
    assert f.filter(_rec("httpx", _BAD)) is False, "全量在 http.log,归档里不重复"
    assert f.filter(_rec("py_clob_client_v2", _OTHER)) is True
    assert f.filter(_rec("engine", "requote")) is True


def test_filter_passes_unparseable_records():
    """解析不出状态码就放行 —— 宁可多打一行,也不能漏掉排障线索。"""
    for keep in (True, False):
        f = _HttpTrafficFilter(keep_failures=keep)
        assert f.filter(_rec("httpx", "HTTP Request: GET https://x/y")) is True


@contextlib.contextmanager
def _restore_logging():
    """configure() 会清空 root handlers 并改级别;测完还原,避免污染其它测试。"""
    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    root_handlers, root_level = list(root.handlers), root.level
    httpx_handlers = list(httpx_logger.handlers)
    try:
        yield
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in root_handlers:
            root.addHandler(h)
        root.setLevel(root_level)
        for h in list(httpx_logger.handlers):
            httpx_logger.removeHandler(h)
            h.close()
        for h in httpx_handlers:
            httpx_logger.addHandler(h)


def test_configure_routes_httpx_to_its_own_file(tmp_path: Path) -> None:
    http_log = tmp_path / "logs" / "http.log"
    json_log = tmp_path / "logs" / "paper.jsonl"
    with _restore_logging():
        configure(json_file=json_log, http_log=http_log, console=True)

        # httpx 的流量行落进自己的文件
        logging.getLogger("httpx").info(_OK)
        logging.getLogger("httpx").info(_BAD)
        for h in logging.getLogger("httpx").handlers:
            h.flush()

        text = http_log.read_text(encoding="utf-8")
        assert "200 OK" in text and "400 Bad Request" in text

        # 归档(JSON)里不再混 httpx 的流量行,但仍记录策略事件
        logging.getLogger("engine").info("requote")
        for h in logging.getLogger().handlers:
            h.flush()
        archived = json_log.read_text(encoding="utf-8")
        assert "requote" in archived
        assert "HTTP Request" not in archived
