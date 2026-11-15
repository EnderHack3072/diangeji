"""Web 层：安全校验、模板转义、Range 解析、真实请求往返。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from diangeji.core.library import LocalLibrary
from diangeji.core.queue import PlayQueue
from diangeji.services.playback import PlaybackService
from diangeji.sources.local_source import LocalSource
from diangeji.sources.registry import SourceRegistry
from diangeji.web.routes import AppContext, _parse_range, build_server
from diangeji.web.security import is_local_request, parse_host, parse_origin
from diangeji.web.views import render_console, render_overlay, render_state_script


# ---------------------------------------------------------------- 安全校验

class TestParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("127.0.0.1:8770", "127.0.0.1"),
        ("localhost:8770", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("[::1]:8770", "::1"),
        ("", ""),
        (None, ""),
    ])
    def test_parse_host(self, raw, expected):
        assert parse_host(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("http://127.0.0.1:8770", "127.0.0.1"),
        ("http://localhost:8770/x", "localhost"),
        ("https://evil.test", "evil.test"),
        ("", ""),
    ])
    def test_parse_origin(self, raw, expected):
        assert parse_origin(raw) == expected


class TestLocalRequest:
    def test_local_page_ok(self):
        assert is_local_request({"Host": "127.0.0.1:8770",
                                 "Origin": "http://127.0.0.1:8770"})

    def test_localhost_ok(self):
        assert is_local_request({"Host": "localhost:8770",
                                 "Referer": "http://localhost:8770/"})

    def test_foreign_origin_rejected(self):
        assert not is_local_request({"Host": "127.0.0.1:8770",
                                     "Origin": "http://evil.test"})

    def test_foreign_host_rejected(self):
        """DNS rebinding：Host 不是回环就拒。"""
        assert not is_local_request({"Host": "evil.test:8770",
                                     "Origin": "http://evil.test"})

    def test_dns_rebinding_host_with_local_origin(self):
        assert not is_local_request({"Host": "attacker.example",
                                     "Origin": "http://127.0.0.1:8770"})

    def test_no_headers_rejected_by_default(self):
        """原实现「两个头都没有就放行」，等于对本机任意进程全开。"""
        assert not is_local_request({"Host": "127.0.0.1:8770"})

    def test_headless_allowed_on_demand(self):
        assert is_local_request({"Host": "127.0.0.1:8770"}, allow_headless=True)

    def test_missing_host_rejected(self):
        assert not is_local_request({"Origin": "http://127.0.0.1:8770"})


# ---------------------------------------------------------------- 模板

class TestViews:
    def test_script_escaping(self):
        payload = render_state_script({"title": "</script><script>alert(1)</script>"})
        assert "</script>" not in payload
        assert "<\\/script>" in payload
        assert json.loads(payload)["title"].startswith("</script>")

    def test_line_separators_escaped(self):
        payload = render_state_script({"x": "a\u2028b\u2029c"})
        assert "\u2028" not in payload and "\u2029" not in payload

    def test_comment_open_escaped(self):
        assert "<!--" not in render_state_script({"x": "<!--"})

    def test_render_console_substitutes(self, tmp_path):
        (tmp_path / "index.html").write_text(
            "<p>__LIBCOUNT__ / __VERSION__</p><script>__STATE__</script>", encoding="utf-8")
        html = render_console(tmp_path, {"a": 1}, 42, "9.9.9")
        assert "42" in html and "9.9.9" in html
        assert '"a": 1' in html and "__STATE__" not in html

    def test_render_overlay(self, tmp_path):
        (tmp_path / "overlay.html").write_text("v__VERSION__", encoding="utf-8")
        assert render_overlay(tmp_path, "1.2.3") == "v1.2.3"


# ---------------------------------------------------------------- Range

class TestRange:
    def test_no_header(self):
        assert _parse_range(None, 1000) == (0, 999)

    def test_open_ended(self):
        assert _parse_range("bytes=100-", 1000) == (100, 999)

    def test_closed(self):
        assert _parse_range("bytes=100-199", 1000) == (100, 199)

    def test_suffix(self):
        """原实现把 ``bytes=-500`` 当成 0-500，播到一半会跳。"""
        assert _parse_range("bytes=-500", 1000) == (500, 999)

    def test_suffix_larger_than_file(self):
        assert _parse_range("bytes=-5000", 1000) == (0, 999)

    def test_start_beyond_size(self):
        assert _parse_range("bytes=2000-", 1000) is None

    def test_empty_file(self):
        assert _parse_range("bytes=0-", 0) is None

    def test_end_clamped(self):
        assert _parse_range("bytes=0-99999", 1000) == (0, 999)

    def test_multi_range_falls_back(self):
        assert _parse_range("bytes=0-10,20-30", 1000) == (0, 999)

    def test_garbage(self):
        assert _parse_range("items=0-10", 1000) == (0, 999)


# ---------------------------------------------------------------- 真实往返

@pytest.fixture()
def live_server(tmp_path, audio_tree):
    """起一个真的 HTTP 服务（端口 0 自动分配），测完关掉。"""
    library = LocalLibrary()
    library.scan([str(audio_tree)])
    registry = SourceRegistry([LocalSource(library)])
    playback = PlaybackService(queue=PlayQueue(idle=False), registry=registry)

    templates = tmp_path / "tpl"
    templates.mkdir()
    (templates / "index.html").write_text(
        "<html>lib=__LIBCOUNT__ v=__VERSION__<script>__STATE__</script></html>",
        encoding="utf-8")
    (templates / "overlay.html").write_text("overlay v__VERSION__", encoding="utf-8")
    images = tmp_path / "img"
    (images / "btn").mkdir(parents=True)
    (images / "bg.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    (images / "btn" / "btn_1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)

    ctx = AppContext(playback=playback, library=library, template_dir=templates,
                     image_dir=images, button_dir=images / "btn", version="0.0.1",
                     allow_headless=True)
    server = build_server(ctx, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, playback
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def get(url, range_header=None):
    request = urllib.request.Request(url)
    if range_header:
        request.add_header("Range", range_header)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, dict(response.headers), response.read()


def post(url, payload=None):
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read() or b"{}")


class TestLiveServer:
    def test_console_page(self, live_server):
        base, playback = live_server
        status, _, body = get(base + "/")
        assert status == 200
        text = body.decode()
        assert "lib=" in text and "0.0.1" in text

    def test_state_api(self, live_server):
        base, playback = live_server
        status, _, body = get(base + "/api/state")
        assert status == 200
        payload = json.loads(body)
        assert "playing" in payload and "queue" in payload

    def test_order_then_state(self, live_server):
        base, playback = live_server
        post(base + "/api/order", {"text": "点歌 晴天", "user": "小明"})
        _, _, body = get(base + "/api/state")
        payload = json.loads(body)
        assert payload["playing"]["title"] == "晴天"
        assert payload["playing"]["user"] == "小明"

    def test_ended_only_advances_matching_id(self, live_server):
        """核心回归：两个页面同时上报 ended，只该切一首。"""
        base, playback = live_server
        post(base + "/api/order", {"text": "点歌 晴天"})
        post(base + "/api/order", {"text": "点歌 晴天娃娃"})
        _, _, body = get(base + "/api/state")
        current = json.loads(body)["playing"]
        assert current["title"] == "晴天"

        # 主播台先报
        _, first = post(base + "/api/ended", {"id": current["id"]})
        assert first["advanced"] is True
        assert first["state"]["playing"]["title"] == "晴天娃娃"

        # OBS 层迟到的那次：ID 已经过期，必须被丢弃
        _, second = post(base + "/api/ended", {"id": current["id"]})
        assert second["advanced"] is False
        assert second["state"]["playing"]["title"] == "晴天娃娃"

    def test_ended_unknown_id_ignored(self, live_server):
        base, playback = live_server
        post(base + "/api/order", {"text": "点歌 晴天"})
        _, body = post(base + "/api/ended", {"id": "deadbeef"})
        assert body["advanced"] is False

    def test_media_full(self, live_server, audio_tree):
        base, playback = live_server
        status, headers, body = get(base + "/media/%E6%99%B4%E5%A4%A9.mp3")
        assert status == 200
        assert headers["Accept-Ranges"] == "bytes"
        assert len(body) == 301 * 1024

    def test_media_range(self, live_server):
        base, playback = live_server
        status, headers, body = get(base + "/media/%E6%99%B4%E5%A4%A9.mp3",
                                    "bytes=0-99")
        assert status == 206
        assert headers["Content-Range"] == f"bytes 0-99/{301 * 1024}"
        assert len(body) == 100

    def test_media_suffix_range(self, live_server):
        base, playback = live_server
        size = 301 * 1024
        status, headers, body = get(base + "/media/%E6%99%B4%E5%A4%A9.mp3",
                                    "bytes=-512")
        assert status == 206
        assert headers["Content-Range"] == f"bytes {size - 512}-{size - 1}/{size}"
        assert len(body) == 512

    def test_media_empty_path_404(self, live_server):
        """GET /media/ 不能把曲库第一首吐出来。"""
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            get(base + "/media/")
        assert info.value.code == 404

    def test_media_substring_404(self, live_server):
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            get(base + "/media/e.mp3")
        assert info.value.code == 404

    def test_media_missing_404(self, live_server):
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            get(base + "/media/nope.mp3")
        assert info.value.code == 404

    def test_asset_png(self, live_server):
        base, playback = live_server
        status, headers, body = get(base + "/asset/bg.png")
        assert status == 200 and headers["Content-Type"] == "image/png"

    def test_asset_button(self, live_server):
        base, playback = live_server
        status, _, _ = get(base + "/asset/btn_1.png")
        assert status == 200

    def test_asset_traversal_blocked(self, live_server):
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            get(base + "/asset/..%2F..%2Fconfig.json")
        assert info.value.code == 404

    def test_volume(self, live_server):
        base, playback = live_server
        _, body = post(base + "/api/volume", {"value": 66})
        assert body["state"]["volume"] == 66

    def test_pause_toggle(self, live_server):
        base, playback = live_server
        post(base + "/api/order", {"text": "点歌 晴天"})
        _, body = post(base + "/api/pause")
        assert body["state"]["paused"] is True

    def test_unknown_api_404(self, live_server):
        base, playback = live_server
        with pytest.raises(urllib.error.HTTPError) as info:
            post(base + "/api/nope")
        assert info.value.code == 404

    def test_overlay_page(self, live_server):
        base, playback = live_server
        status, _, body = get(base + "/overlay")
        assert status == 200 and b"overlay v0.0.1" in body


class TestForbidden:
    """关掉 allow_headless 后，没有 Origin/Referer 的请求必须被拒。"""

    def test_api_forbidden_without_origin(self, tmp_path):
        library = LocalLibrary()
        playback = PlaybackService(queue=PlayQueue(idle=False),
                                   registry=SourceRegistry([LocalSource(library)]))
        templates = tmp_path / "t"
        templates.mkdir()
        (templates / "index.html").write_text("x", encoding="utf-8")
        (templates / "overlay.html").write_text("y", encoding="utf-8")
        ctx = AppContext(playback=playback, library=library, template_dir=templates,
                         image_dir=tmp_path, button_dir=tmp_path, allow_headless=False)
        server = build_server(ctx, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with pytest.raises(urllib.error.HTTPError) as info:
                get(base + "/api/state")
            assert info.value.code == 403
            # 页面本身仍可访问（不然连界面都打不开）
            assert get(base + "/")[0] == 200
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_api_allowed_with_local_origin(self, tmp_path):
        library = LocalLibrary()
        playback = PlaybackService(queue=PlayQueue(idle=False),
                                   registry=SourceRegistry([LocalSource(library)]))
        templates = tmp_path / "t"
        templates.mkdir()
        (templates / "index.html").write_text("x", encoding="utf-8")
        (templates / "overlay.html").write_text("y", encoding="utf-8")
        ctx = AppContext(playback=playback, library=library, template_dir=templates,
                         image_dir=tmp_path, button_dir=tmp_path)
        server = build_server(ctx, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/state")
            request.add_header("Origin", f"http://127.0.0.1:{port}")
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
