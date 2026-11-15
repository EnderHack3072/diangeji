"""直播层：协议打包/解包、WBI 签名、点歌指令解析。"""

from __future__ import annotations

import json
import struct
import zlib

import pytest

from diangeji.live import protocol
from diangeji.live.commands import Order, is_order, parse_order
from diangeji.live.wbi import WBISigner, _sanitize


# ---------------------------------------------------------------- 协议

class TestProtocol:
    def test_header_roundtrip(self):
        packed = protocol.pack(protocol.OP_AUTH, b'{"roomid":1}')
        packet = next(protocol.iter_packets(packed))
        assert packet.opcode == protocol.OP_AUTH
        assert packet.body == b'{"roomid":1}'
        assert packet.version == protocol.VER_PLAIN
        assert packet.total == protocol.HEADER_SIZE + 12

    def test_partial_packet_ignored(self):
        """半包不产出，等下次再接（客户端负责继续追加缓冲区）。"""
        packed = protocol.pack(protocol.OP_HEARTBEAT, b"payload")
        assert list(protocol.iter_packets(packed[:-3])) == []

    def test_two_packets_in_one_buffer(self):
        payload = (protocol.pack(protocol.OP_HEARTBEAT, b"a")
                   + protocol.pack(protocol.OP_HEARTBEAT, b"b"))
        assert [p.body for p in protocol.iter_packets(payload)] == [b"a", b"b"]

    def test_too_small_total_stops(self):
        """``total < 16`` 若继续解释会让游标倒退/原地打转。必须停。"""
        bad = struct.pack(">IHHII", 4, 16, 0, 1, 0) + b"\0" * 20
        assert list(protocol.iter_packets(bad)) == []

    def test_oversized_body_rejected(self):
        """声明长度超出实际缓冲 → 停，不猜。"""
        bad = struct.pack(">IHHII", 9999, 16, 0, 1, 0)
        assert list(protocol.iter_packets(bad)) == []

    def test_zero_length_body(self):
        packet = next(protocol.iter_packets(protocol.pack(protocol.OP_HEARTBEAT)))
        assert packet.body == b""

    def test_auth_payload_is_packed_json(self):
        """``auth_payload`` 返回的是**打包好的包**，不是裸 JSON。"""
        packet = next(protocol.iter_packets(protocol.auth_payload(12345)))
        assert packet.opcode == protocol.OP_AUTH
        body = json.loads(packet.body)
        assert body["roomid"] == 12345
        assert body["uid"] == 0
        assert body["platform"] == "web"

    def test_auth_payload_version_reported(self):
        packet = next(protocol.iter_packets(protocol.auth_payload(1, version=2)))
        assert json.loads(packet.body)["protover"] == 2

    def test_auth_payload_uid_and_token(self):
        packet = next(protocol.iter_packets(
            protocol.auth_payload(7, token="tok", uid=888)))
        body = json.loads(packet.body)
        assert body["uid"] == 888 and body["key"] == "tok"

    def test_heartbeat_is_packed_and_empty(self):
        packet = next(protocol.iter_packets(protocol.heartbeat_payload()))
        assert packet.opcode == protocol.OP_HEARTBEAT
        assert packet.body == b""

    def test_decode_plain_body(self):
        assert protocol.decode_body(protocol.VER_PLAIN, b'{"a":1}') == b'{"a":1}'

    def test_zlib_body(self):
        raw = b"hello world" * 10
        assert protocol.decode_body(protocol.VER_ZLIB, zlib.compress(raw)) == raw

    def test_bad_zlib_returns_none(self):
        # 解不开就放弃这一包，别把压缩流当 JSON 喂下去
        assert protocol.decode_body(protocol.VER_ZLIB, b"not compressed") is None

    def test_brotli_when_available(self):
        brotli = pytest.importorskip("brotli")
        raw = b"payload" * 20
        assert protocol.decode_body(protocol.VER_BROTLI, brotli.compress(raw)) == raw

    def test_protover_matches_brotli_availability(self):
        assert protocol.protover() == (protocol.VER_BROTLI
                                       if protocol.BROTLI_AVAILABLE else protocol.VER_ZLIB)

    def test_iter_messages_flat(self):
        payload = protocol.pack(protocol.OP_MESSAGE,
                                json.dumps({"cmd": "DANMU_MSG"}).encode())
        assert list(protocol.iter_messages(payload)) == [{"cmd": "DANMU_MSG"}]

    def test_iter_messages_ignores_non_message_opcode(self):
        payload = protocol.pack(protocol.OP_HEARTBEAT_REPLY, b"12345")
        assert list(protocol.iter_messages(payload)) == []

    def test_iter_messages_expands_nested_zlib(self):
        inner = protocol.pack(protocol.OP_MESSAGE, b'{"cmd":"X"}')
        outer = protocol.pack(protocol.OP_MESSAGE, zlib.compress(inner),
                              version=protocol.VER_ZLIB)
        assert list(protocol.iter_messages(outer)) == [{"cmd": "X"}]

    def test_iter_messages_skips_bad_json(self):
        payload = protocol.pack(protocol.OP_MESSAGE, b"{not json")
        assert list(protocol.iter_messages(payload)) == []


# ---------------------------------------------------------------- WBI

class TestWBI:
    @staticmethod
    def signer() -> WBISigner:
        signer = WBISigner(clock=lambda: 1000.0)
        signer._mixin = "0123456789abcdef0123456789abcdef"  # 绕过网络取 key
        signer._stamp = 1000.0
        return signer

    def test_signature_stable(self):
        signer = self.signer()
        first = signer.sign("/x", {"room_id": 1}, timestamp=1700000000)
        second = signer.sign("/x", {"room_id": 1}, timestamp=1700000000)
        assert first == second

    def test_param_order_does_not_matter(self):
        signer = self.signer()
        assert (signer.sign("/x", {"a": 1, "b": 2}, timestamp=5)
                == signer.sign("/x", {"b": 2, "a": 1}, timestamp=5))

    def test_contains_wts_and_w_rid(self):
        signer = self.signer()
        url = signer.sign("/x", {"room_id": 1}, timestamp=123)
        assert "wts=123" in url
        assert "w_rid=" in url
        assert len(url.rsplit("w_rid=", 1)[-1]) == 32

    def test_bad_chars_stripped(self):
        """B 站 WBI 要求参数值去掉 !'()* —— 少了这步签名必错。"""
        assert _sanitize("a!b'c(d)e*f") == "abcdef"

    def test_sanitize_applied_to_params(self):
        signer = self.signer()
        assert (signer.sign("/x", {"q": "a!b"}, timestamp=1)
                == signer.sign("/x", {"q": "ab"}, timestamp=1))

    def test_mixin_key_cached(self):
        """同一实例重复取 key 不该重复打网络。"""
        calls = []

        def fake_get_json(url, **kwargs):
            calls.append(url)
            return {"data": {"wbi_img": {
                "img_url": "https://i.test/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
                "sub_url": "https://i.test/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png"}}}

        import diangeji.live.wbi as wbi_module
        original = wbi_module.get_json
        wbi_module.get_json = fake_get_json
        try:
            signer = WBISigner(clock=lambda: 100.0)
            first = signer.mixin_key()
            second = signer.mixin_key()
        finally:
            wbi_module.get_json = original
        assert first == second and len(calls) == 1
        assert first is not None and len(first) == 32

    def test_invalidate_forces_refetch(self):
        signer = self.signer()
        signer.invalidate()
        assert signer._mixin is None

    def test_no_key_returns_url_unchanged(self):
        """拿不到 mixin key 时降级：原样返回，由调用方兜底，别抛异常。"""
        signer = WBISigner(clock=lambda: 0.0)
        signer._mixin = None
        import diangeji.live.wbi as wbi_module
        original = wbi_module.get_json
        wbi_module.get_json = lambda *a, **k: {"code": -1}
        try:
            assert signer.sign("/x", {"a": 1}) == "/x"
        finally:
            wbi_module.get_json = original


# ---------------------------------------------------------------- 指令

class TestCommands:
    @pytest.mark.parametrize("text", [
        "点歌 晴天", "点播 晴天", "来一首晴天", "想听 晴天", "点歌晴天",
        "点歌：晴天", "点歌，晴天", "  点歌 晴天  ",
        "点歌 周杰伦 晴天", "点歌《晴天》",
    ])
    def test_recognised(self, text):
        assert is_order(text)

    @pytest.mark.parametrize("text", [
        "你好", "点歌", "点歌   ", "", "这首真好听", "点什么歌",
    ])
    def test_not_order(self, text):
        assert not is_order(text)

    def test_keyword_extracted(self):
        order = parse_order("点歌 周杰伦 晴天")
        assert order is not None
        assert order.keyword == "周杰伦 晴天"

    def test_trailing_punctuation_stripped(self):
        order = parse_order("点歌 晴天！")
        assert order is not None and order.keyword == "晴天"

    def test_colon_forms(self):
        assert parse_order("点歌：晴天").keyword == "晴天"
        assert parse_order("点播,晴天").keyword == "晴天"

    def test_raw_text_kept(self):
        order = parse_order("  点歌 晴天  ")
        assert order is not None and order.raw_text == "点歌 晴天"

    def test_overlong_rejected(self):
        """超长弹幕多半是刷屏，不当点歌处理。"""
        assert parse_order("点歌 " + "啊" * 200) is None

    def test_exactly_at_limit_kept(self):
        assert parse_order("点歌 " + "啊" * 60) is not None

    def test_order_is_dataclass(self):
        order = parse_order("点歌 晴天")
        assert isinstance(order, Order)
        assert order.keyword == "晴天"
