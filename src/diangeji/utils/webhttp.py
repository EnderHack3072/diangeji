"""JSON over HTTP 的薄封装：统一 UA / 超时 / 编码 / 错误处理。

只用标准库。所有网络异常的收敛点在这里，业务代码拿到的要么是数据，要么是
``None``——不向外抛 ``URLError`` 之类的底层异常。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from ..logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = 10.0


class HttpError(RuntimeError):
    """网络请求失败（已包装底层异常）。"""


def _build(url: str, data: bytes | None, headers: Mapping[str, str] | None) -> urllib.request.Request:
    return urllib.request.Request(url, data=data, headers=dict(headers or {}))


def request(url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None,
            timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """发请求读原始字节。失败抛 ``HttpError``。"""
    try:
        with urllib.request.urlopen(_build(url, data, headers), timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HttpError(f"{url}: {exc}") from exc


def get_json(url: str, *, headers: Mapping[str, str] | None = None,
             timeout: float = DEFAULT_TIMEOUT, parse_error_ok: bool = False) -> Any:
    """GET 并解析 JSON。失败返回 ``None``（除非 ``parse_error_ok=False`` 且要抛）。"""
    try:
        raw = request(url, headers=headers, timeout=timeout)
    except HttpError as exc:
        log.debug("GET 失败：%s", exc)
        return None
    return _loads(raw)


def post_json(url: str, payload: Any, *, headers: Mapping[str, str] | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST JSON 并解析 JSON 响应。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(dict(headers or {}))
    try:
        raw = request(url, data=body, headers=merged, timeout=timeout)
    except HttpError as exc:
        log.debug("POST 失败：%s", exc)
        return None
    return _loads(raw)


def _loads(raw: bytes, encodings: tuple[str, ...] = ("utf-8", "gbk")) -> Any:
    """按候选编码依次尝试解析（QQ 音乐部分老接口会吐 GBK）。"""
    for enc in encodings:
        try:
            return json.loads(raw.decode(enc, "ignore"))
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def with_query(url: str, params: Mapping[str, str]) -> str:
    """拼查询串（不排序，需要签名时由调用方自己排）。"""
    joiner = "&" if urllib.parse.urlparse(url).query else "?"
    return url + joiner + urllib.parse.urlencode(params)


def host_of(url: str) -> str:
    """取主机名，非法 URL 返回空串。"""
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
