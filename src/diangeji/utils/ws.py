"""极简 WebSocket 客户端（RFC 6455），只用标准库。

原 ``wsclient.py`` 能用，但有三处隐患，这里一并处理：

* 帧长度没有上限 —— 恶意/异常端点发个 8EB 的帧头就能把进程吃死。现在超过
  ``MAX_FRAME_BYTES`` 直接断连。
* ``buf += chunk`` 拼接是 O(n²)。改成 ``bytearray`` + 内存视图。
* ping 处理用递归，两边疯狂 ping 会打穿栈。改成循环。

对外接口保持极简：``connect() / send(data, opcode) / recv() -> (opcode, payload) / close()``。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import urllib.parse
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

#: 单帧上限（16 MB，B 站弹幕包远小于此）
MAX_FRAME_BYTES = 16 * 1024 * 1024
#: 握手响应头上限
MAX_HEADER_BYTES = 64 * 1024

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(ConnectionError):
    """握手或帧解析失败。"""


class WSClient:
    """阻塞式 WebSocket 客户端。不做自动重连 —— 那是调用方的事。"""

    def __init__(self, url: str, origin: str | None = None,
                 headers: dict[str, str] | None = None) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"不支持的协议：{parsed.scheme}")
        self.url = url
        self.secure = parsed.scheme == "wss"
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if self.secure else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.origin = origin
        self.extra_headers = dict(headers or {})
        self.sock: socket.socket | ssl.SSLSocket | None = None
        self._buf = bytearray()

    # ---------------- 握手 ----------------

    def connect(self, timeout: float = 10.0) -> "WSClient":
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            if self.secure:
                context = ssl.create_default_context()
                raw = context.wrap_socket(raw, server_hostname=self.host)
            self.sock = raw

            key = base64.b64encode(os.urandom(16)).decode("ascii")
            lines = [
                f"GET {self.path} HTTP/1.1",
                f"Host: {self.host}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            if self.origin:
                lines.append(f"Origin: {self.origin}")
            for name, value in self.extra_headers.items():
                lines.append(f"{name}: {value}")
            raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))

            head = self._read_header()
            status_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            if " 101 " not in status_line:
                raise WebSocketError(f"握手失败：{status_line}")
            self._verify_accept(head, key)
            return self
        except BaseException:
            # 握手任何一步失败都要关掉 socket，否则重连循环会一直漏 fd
            self.close(graceful=False)
            raise

    def _read_header(self) -> bytes:
        assert self.sock is not None
        buffered = bytearray()
        while b"\r\n\r\n" not in buffered:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手失败：连接被关闭")
            buffered += chunk
            if len(buffered) > MAX_HEADER_BYTES:
                raise WebSocketError("握手响应头过大")
        head, _, rest = bytes(buffered).partition(b"\r\n\r\n")
        self._buf = bytearray(rest)
        return head

    def _verify_accept(self, head: bytes, key: str) -> None:
        """校验 ``Sec-WebSocket-Accept``，防止被中间人糊弄。"""
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() != b"sec-websocket-accept":
                continue
            expect = base64.b64encode(
                hashlib.sha1((key + _GUID).encode("ascii")).digest()
            ).decode("ascii")
            if value.strip().decode("latin1", "replace") != expect:
                raise WebSocketError("握手校验失败：Sec-WebSocket-Accept 不匹配")
            return
        raise WebSocketError("握手失败：缺少 Sec-WebSocket-Accept")

    # ---------------- 收发 ----------------

    def _recv_exact(self, size: int) -> bytes:
        assert self.sock is not None
        while len(self._buf) < size:
            chunk = self.sock.recv(max(65536, size - len(self._buf)))
            if not chunk:
                raise WebSocketError("连接已关闭")
            self._buf += chunk
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def send(self, data: bytes | str, opcode: int = OP_TEXT) -> None:
        """发一条已掩码的帧（客户端必须掩码）。"""
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        assert self.sock is not None
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> tuple[int, bytes]:
        """收一条帧。控制帧（ping/pong）在内部消化掉，返回业务帧。"""
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            if length > MAX_FRAME_BYTES:
                raise WebSocketError(f"帧过大：{length} 字节")
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == OP_PING:
                self._send_control(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            return opcode, payload

    def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        if not payload or len(payload) > 125:
            payload = b""
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode, 0x80 | len(payload)]) + mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        assert self.sock is not None
        try:
            self.sock.sendall(bytes(header) + masked)
        except OSError:
            pass

    def set_timeout(self, seconds: float) -> None:
        if self.sock is not None:
            self.sock.settimeout(seconds)

    def close(self, graceful: bool = True) -> None:
        """关闭连接。``graceful=False`` 用于握手失败时 —— 那时发 CLOSE 帧没有意义。"""
        try:
            if self.sock is not None:
                if graceful:
                    self._send_control(OP_CLOSE)
                self.sock.close()
        except OSError:
            pass
        finally:
            self.sock = None

    def __enter__(self) -> "WSClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
