"""B 站弹幕协议：16 字节包头 + 可嵌套的压缩包（protover 2/3）。"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from typing import Iterator

try:  # 可选依赖：没有 brotli 就只用 protover 2
    import brotli as _brotli
except ImportError:  # pragma: no cover - 环境相关
    _brotli = None

#: 包长(uint32) 头长(uint16) 协议版本(uint16) 操作码(uint32) 序号(uint32)
HEADER = struct.Struct(">IHHII")
HEADER_SIZE = HEADER.size  # 16

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

VER_PLAIN = 0
VER_HEARTBEAT = 1
VER_ZLIB = 2
VER_BROTLI = 3

BROTLI_AVAILABLE = _brotli is not None


def protover() -> int:
    """能解 brotli 就报 3，否则让服务端发 zlib。"""
    return VER_BROTLI if BROTLI_AVAILABLE else VER_ZLIB


def pack(opcode: int, body: bytes = b"", version: int = VER_PLAIN,
         sequence: int = 1) -> bytes:
    """拼一个完整包。"""
    return HEADER.pack(HEADER_SIZE + len(body), HEADER_SIZE, version, opcode,
                       sequence) + body


def auth_payload(room_id: int, token: str = "", uid: int = 0,
                 version: int | None = None) -> bytes:
    return pack(OP_AUTH, json.dumps({
        "uid": uid,
        "roomid": room_id,
        "protover": version if version is not None else protover(),
        "platform": "web",
        "type": 2,
        "key": token,
    }).encode("utf-8"))


def heartbeat_payload() -> bytes:
    return pack(OP_HEARTBEAT)


@dataclass(slots=True)
class Packet:
    total: int
    version: int
    opcode: int
    body: bytes


def iter_packets(payload: bytes) -> Iterator[Packet]:
    """把一段字节切成一串包。

    边界处理是这块最容易写错的地方，保持原版的正确性：
    ``total < 16``（长度非法）或越界时立刻停下，而不是继续解释后面的垃圾数据。
    """
    pos = 0
    end = len(payload)
    while pos + HEADER_SIZE <= end:
        total, _, version, opcode, _ = HEADER.unpack_from(payload, pos)
        if total < HEADER_SIZE or pos + total > end:
            break
        body = payload[pos + HEADER_SIZE:pos + total]
        pos += total
        yield Packet(total=total, version=version, opcode=opcode, body=body)


def decode_body(version: int, body: bytes) -> bytes | None:
    """按协议版本解压；不认识的版本返回 ``None``。"""
    if version == VER_ZLIB:
        try:
            return zlib.decompress(body)
        except zlib.error:
            return None
    if version == VER_BROTLI:
        if _brotli is None:
            return None
        try:
            return _brotli.decompress(body)
        except Exception:
            return None
    return body


def iter_messages(payload: bytes) -> Iterator[dict]:
    """解出所有 ``DANMU_MSG`` 之外的业务 JSON（含嵌套压缩包）。"""
    for packet in iter_packets(payload):
        if packet.opcode != OP_MESSAGE:
            continue
        if packet.version in (VER_ZLIB, VER_BROTLI):
            inner = decode_body(packet.version, packet.body)
            if inner is None:
                continue
            yield from iter_messages(inner)
            continue
        try:
            yield json.loads(packet.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
