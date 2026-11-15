"""扫码登录：B 站 / QQ 音乐。

拆成「取二维码 → 轮询状态 → 产出 cookie」三段，每段都可单独测。
二维码渲染交给 ``ui``（这里只给点阵或 PNG 字节），所以本模块**不依赖 tkinter**。

``qrcode`` / ``pypng`` 是可选依赖：缺了也能跑主程序，只是扫码登录不可用
（可以改用浏览器登录或手动粘贴 cookie）。
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..constants import BILI_LIVE_URL, QQ_MUSIC_URL, UA_EDGE
from ..logging_setup import get_logger
from .store import normalize_cookie

log = get_logger(__name__)


class LoginResult(str, Enum):
    """轮询状态。"""

    WAITING = "waiting"    # 还没扫
    SCANNED = "scanned"    # 已扫待确认
    OK = "ok"              # 成功，cookie 可以取
    EXPIRED = "expired"    # 二维码过期


@dataclass
class QRPayload:
    """二维码内容。``png`` 有值时界面直接画图，否则用 ``matrix`` 点阵。"""

    text: str = ""
    png: bytes | None = None
    matrix: list[list[bool]] | None = None
    error: str = ""


def _opener(jar: http.cookiejar.CookieJar):
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _request(url: str, referer: str | None = None, data: bytes | None = None,
             extra: dict[str, str] | None = None) -> urllib.request.Request:
    headers = {"User-Agent": UA_EDGE, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    if extra:
        headers.update(extra)
    return urllib.request.Request(url, data=data, headers=headers)


def _jar_text(jar: http.cookiejar.CookieJar, names: set[str] | None = None) -> str:
    out = []
    for cookie in jar:
        if names is None or cookie.name in names:
            out.append(f"{cookie.name}={cookie.value}")
    return "; ".join(out)


# ---------------------------------------------------------------- 二维码渲染

def qr_matrix(text: str, border: int = 2) -> list[list[bool]]:
    """二维码点阵（``True`` 为黑）。缺依赖抛 ``RuntimeError``。"""
    try:
        import qrcode
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RuntimeError("需要 qrcode 包才能扫码登录（pip install 'diangeji[qr]'）") from exc
    code = qrcode.QRCode(border=border, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(text)
    code.make(fit=True)
    return code.get_matrix()


def png_matrix(payload: bytes) -> list[list[bool]]:
    """把二维码 PNG 解回点阵（不依赖 Pillow）。"""
    try:
        import png
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 pypng 包") from exc
    reader = png.Reader(bytes=payload)
    width, height, rows, info = reader.asDirect()
    row_list = list(rows)
    if row_list and isinstance(row_list[0], list):
        row_list = [[value for pixel in row for value in pixel] for row in row_list]

    maxval: Any = info.get("maxval") if isinstance(info, dict) else None
    if isinstance(maxval, (list, tuple)):
        maxval = maxval[0]
    one_bit = maxval in (1, None) and max(max(row[:1]) for row in row_list) <= 1
    if one_bit:
        matrix = [[row_list[y][x] == 0 for x in range(width)] for y in range(height)]
    else:
        matrix = [[row_list[y][x] < 128 for x in range(width)] for y in range(height)]
    return _trim_and_pad(matrix)


def _trim_and_pad(matrix: list[list[bool]], border: int = 2) -> list[list[bool]]:
    """去掉外层白边再补回固定白边，保证不同来源的码尺寸一致。"""
    if not matrix:
        return matrix
    top, bottom = 0, len(matrix) - 1
    left, right = 0, len(matrix[0]) - 1
    while top < bottom and not any(matrix[top]):
        top += 1
    while bottom > top and not any(matrix[bottom]):
        bottom -= 1
    while left < right and not any(matrix[y][left] for y in range(top, bottom + 1)):
        left += 1
    while right > left and not any(matrix[y][right] for y in range(top, bottom + 1)):
        right -= 1
    core = [row[left:right + 1] for row in matrix[top:bottom + 1]]
    width = len(core[0])
    pad = [[False] * (width + border * 2) for _ in range(border)]
    body = [[False] * border + row + [False] * border for row in core]
    return pad + body + [row[:] for row in pad]




# ---------------------------------------------------------------- B 站

class BiliQRLogin:
    """passport.bilibili.com 扫码：86101 未扫 / 86090 已扫待确认 / 0 成功 / 86038 过期。"""

    GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={key}"
    COOKIE_KEYS = {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "buvid4"}

    name = "B 站"

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = _opener(self.jar)
        self.key = ""
        self.url = ""

    def start(self) -> QRPayload:
        try:
            response = self.opener.open(
                _request(self.GENERATE, referer="https://www.bilibili.com/"), timeout=15)
            data = json.load(response)
        except Exception as exc:
            return QRPayload(error=f"取二维码失败：{exc}")
        if data.get("code") != 0:
            return QRPayload(error=f"取二维码失败：{data.get('message') or data.get('code')}")
        body = data.get("data") or {}
        self.key = str(body.get("qrcode_key") or "")
        self.url = str(body.get("url") or "")
        payload = QRPayload(text=self.url)
        try:
            payload.matrix = qr_matrix(self.url, border=2)
        except RuntimeError as exc:
            payload.error = str(exc)
        return payload

    def poll(self) -> tuple[LoginResult, str | None]:
        if not self.key:
            return LoginResult.EXPIRED, None
        try:
            response = self.opener.open(
                _request(self.POLL.format(key=self.key), referer="https://www.bilibili.com/"),
                timeout=15)
            data = json.load(response)
        except Exception as exc:
            log.debug("轮询 B 站登录状态失败：%s", exc)
            return LoginResult.WAITING, None

        code = (data.get("data") or {}).get("code")
        if code == 0:
            try:  # 顺手抓一次主页，补 buvid
                self.opener.open(_request(BILI_LIVE_URL, referer="https://www.bilibili.com/"),
                                 timeout=10).read()
            except Exception:
                pass
            text = normalize_cookie(_jar_text(self.jar, self.COOKIE_KEYS))
            if "SESSDATA" not in text:
                log.warning("扫码成功但没拿到 SESSDATA")
                return LoginResult.EXPIRED, None
            return LoginResult.OK, text
        if code == 86038:
            return LoginResult.EXPIRED, None
        if code == 86090:
            return LoginResult.SCANNED, None
        return LoginResult.WAITING, None


# ---------------------------------------------------------------- QQ 音乐

class QQMusicQRLogin:
    """ptlogin2.qq.com 扫码，成功后换 y.qq.com 的 qm_keyst。"""

    APPID = "716027609"
    DAID = "383"
    THIRD_AID = "100497308"

    name = "QQ 音乐"

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = _opener(self.jar)
        self.token = 0
        self.png: bytes | None = None

    @staticmethod
    def _u1() -> str:
        return urllib.parse.quote("https://graph.qq.com/oauth2.0/login_jump", safe="")

    def start(self) -> QRPayload:
        url = ("https://ssl.ptlogin2.qq.com/ptqrshow?appid=%s&e=2&l=M&s=3&d=72&v=4&t=%s"
               "&daid=%s&pt_3rd_aid=%s&u1=%s") % (
            self.APPID, time.time(), self.DAID, self.THIRD_AID, self._u1())
        try:
            response = self.opener.open(_request(url, referer="https://xui.ptlogin2.qq.com/"),
                                        timeout=15)
            self.png = response.read()
        except Exception as exc:
            return QRPayload(error=f"取二维码失败：{exc}")
        sig = next((c.value for c in self.jar if c.name == "qrsig"), None)
        if not sig:
            return QRPayload(error="没拿到 qrsig，接口可能变了")
        self.token = _hash33(sig)
        payload = QRPayload(png=self.png)
        try:
            payload.matrix = png_matrix(self.png)
        except RuntimeError as exc:
            payload.error = str(exc)
        return payload

    def poll(self) -> tuple[LoginResult, str | None]:
        url = ("https://ssl.ptlogin2.qq.com/ptqrlogin?u1=%s&ptqrtoken=%s&ptredirect=1&h=1&t=1&g=1"
               "&from_ui=1&ptlang=2052&action=0-0-%s&js_ver=20102616&js_type=1&login_sig="
               "&pt_uistyle=40&aid=%s&daid=%s&pt_3rd_aid=%s&has_onekey=1"
               % (self._u1(), self.token, int(time.time() * 1000), self.APPID, self.DAID,
                  self.THIRD_AID))
        try:
            response = self.opener.open(_request(url, referer="https://xui.ptlogin2.qq.com/"),
                                        timeout=15)
            text = response.read().decode("utf-8", "ignore")
        except Exception as exc:
            log.debug("轮询 QQ 登录状态失败：%s", exc)
            return LoginResult.WAITING, None

        match = re.search(r"ptuiCB\('(\d+)','(\d+)','([^']*)'", text)
        if not match:
            return LoginResult.WAITING, None
        code = match.group(1)
        if code == "65":
            return LoginResult.EXPIRED, None
        if code == "67":
            return LoginResult.SCANNED, None
        if code != "0":
            return LoginResult.WAITING, None

        self._follow(match.group(3).replace("\\/", "/"))
        for page in (QQ_MUSIC_URL, "https://y.qq.com/portal/profile/",
                     "https://y.qq.com/n/ryqq/profile"):
            try:
                self.opener.open(_request(page, referer=QQ_MUSIC_URL), timeout=15).read()
            except Exception:
                continue
            if "qm_keyst" in _jar_text(self.jar, {"qm_keyst"}):
                break
        text = normalize_cookie(_jar_text(
            self.jar, {"qm_keyst", "qqmusic_key", "uin", "p_uin",
                       "psrf_musickey_createtime", "psrf_qqunionid"}))
        if "qm_keyst" not in text:
            names = ", ".join(sorted({c.name for c in self.jar}))[:220]
            log.warning("扫码成功但没换到 qm_keyst，已拿到：%s", names)
            return LoginResult.EXPIRED, None
        return LoginResult.OK, text

    def _follow(self, url: str, depth: int = 0) -> None:
        """跟着跳转链走，直到出现 qm_keyst。"""
        if depth > 6 or not url:
            return
        try:
            response = self.opener.open(_request(url, referer=QQ_MUSIC_URL), timeout=15)
            body = response.read().decode("utf-8", "ignore")
        except Exception:
            return
        if "qm_keyst" in _jar_text(self.jar, {"qm_keyst"}):
            return
        match = re.search(
            r'(?:location\.href|location\.replace|window\.location)\s*(?:=|\()\s*["\']([^"\']+)',
            body)
        if not match:
            match = re.search(r'http-equiv=["\']?refresh["\']?[^>]+url=([^"\'>]+)', body, re.I)
        if not match:
            return
        nxt = match.group(1).replace("\\/", "/")
        if nxt.startswith("//"):
            nxt = "https:" + nxt
        self._follow(urllib.parse.urljoin(url, nxt), depth + 1)


def _hash33(text: str) -> int:
    """QQ 的 ``hash33``：轮询 token 的算法。"""
    value = 0
    for char in text:
        value += (value << 5) + ord(char)
    return value & 0x7FFFFFFF
