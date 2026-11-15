"""基础层：cookie 存储、配置读写、路径、文本工具、HTTP 小工具。"""

from __future__ import annotations

import json
import os
import sys
import threading

import pytest

from diangeji import paths
from diangeji.auth.store import (
    BILI_COOKIE,
    QQ_COOKIE,
    REQUIRED_KEYS,
    CookieStore,
    normalize_cookie,
    split_pairs,
)
from diangeji.config import Config
from diangeji.utils import text as text_utils
from diangeji.utils import timeutil
from diangeji.utils.webhttp import host_of, with_query


# ---------------------------------------------------------------- cookie

class TestNormalize:
    def test_from_string(self):
        assert normalize_cookie("SESSDATA=abc; bili_jct=def") == "SESSDATA=abc; bili_jct=def"

    def test_from_mapping(self):
        """原 bug 的根源：把 dict 直接喂给 ``write`` 再 ``f.write``。"""
        assert normalize_cookie({"SESSDATA": "abc"}) == "SESSDATA=abc"

    def test_strips_whitespace(self):
        assert normalize_cookie(" a = 1 ; b = 2 ") == "a=1; b=2"

    def test_skips_empty_values(self):
        assert normalize_cookie("a=; b=2") == "b=2"

    def test_skips_valueless(self):
        assert normalize_cookie("a; b=2") == "b=2"

    def test_handles_equals_in_value(self):
        assert normalize_cookie("k=a=b") == "k=a=b"

    def test_none_and_empty(self):
        assert normalize_cookie(None) == ""
        assert normalize_cookie("") == ""

    def test_split_pairs(self):
        assert split_pairs("a=1; b=2") == [("a", "1"), ("b", "2")]


class TestCookieStore:
    def test_write_then_read(self, data_dir):
        store = CookieStore(data_dir)
        assert store.write(BILI_COOKIE, "SESSDATA=abc; bili_jct=def") is True
        assert "SESSDATA=abc" in store.read(BILI_COOKIE)
        assert store.has(BILI_COOKIE) is True

    def test_write_rejects_empty(self, data_dir):
        """空 cookie 写盘会把好文件清空 —— 必须拒。"""
        store = CookieStore(data_dir)
        store.write(BILI_COOKIE, "SESSDATA=keep")
        assert store.write(BILI_COOKIE, "") is False
        assert store.write(BILI_COOKIE, {}) is False
        assert "SESSDATA=keep" in store.read(BILI_COOKIE)

    def test_write_accepts_dict(self, data_dir):
        store = CookieStore(data_dir)
        assert store.write(QQ_COOKIE, {"qm_keyst": "x", "uin": "1"}) is True
        text = store.read(QQ_COOKIE)
        assert "qm_keyst=x" in text and "uin=1" in text

    def test_read_missing_file(self, data_dir):
        assert CookieStore(data_dir).read("nope.txt") == ""

    def test_has_requires_required_keys(self, data_dir):
        store = CookieStore(data_dir)
        store.write(QQ_COOKIE, "uin=1")           # 缺 qm_keyst
        assert store.has(QQ_COOKIE) is False
        store.write(QQ_COOKIE, "qm_keyst=k; uin=1")
        assert store.has(QQ_COOKIE) is True

    def test_value(self, data_dir):
        store = CookieStore(data_dir)
        store.write(BILI_COOKIE, "SESSDATA=abc; bili_jct=def")
        assert store.value(BILI_COOKIE, "SESSDATA") == "abc"
        assert store.value(BILI_COOKIE, "missing") == ""

    def test_merge(self, data_dir):
        store = CookieStore(data_dir)
        store.write(BILI_COOKIE, "SESSDATA=abc")
        store.merge(BILI_COOKIE, {"buvid3": "xyz"})
        assert store.value(BILI_COOKIE, "SESSDATA") == "abc"
        assert store.value(BILI_COOKIE, "buvid3") == "xyz"

    def test_clear(self, data_dir):
        store = CookieStore(data_dir)
        store.write(BILI_COOKIE, "SESSDATA=abc")
        store.clear(BILI_COOKIE)
        assert store.read(BILI_COOKIE) == ""

    def test_write_is_atomic(self, data_dir):
        """中途失败也不该留下半截文件。"""
        store = CookieStore(data_dir)
        store.write(BILI_COOKIE, "SESSDATA=ok")
        for _ in range(20):
            assert store.write(BILI_COOKIE, "SESSDATA=again; x=1") is True
            assert "SESSDATA=again" in store.read(BILI_COOKIE)

    def test_concurrent_writes_do_not_corrupt(self, data_dir):
        store = CookieStore(data_dir)
        store.write(BILI_COOKIE, "SESSDATA=init")

        def worker(index: int) -> None:
            for _ in range(10):
                store.write(BILI_COOKIE, f"SESSDATA=v{index}; x=1")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        text = store.read(BILI_COOKIE)
        assert text.startswith("SESSDATA=v")
        assert "=" in text

    def test_no_temp_files_left(self, data_dir):
        store = CookieStore(data_dir)
        for _ in range(5):
            store.write(BILI_COOKIE, "SESSDATA=abc")
        leftovers = [n for n in os.listdir(data_dir) if n.startswith(".tmp")]
        assert leftovers == []


# ---------------------------------------------------------------- 配置

class TestConfig:
    def test_defaults(self):
        cfg = Config()
        assert cfg.volume == 20
        assert cfg.auto_start is False
        assert cfg.local_dirs == []

    def test_roundtrip(self, data_dir):
        cfg = Config(room_id="12345", volume=55, idle_enabled=True,
                     local_dirs=["D:/music"], auto_start=True)
        path = data_dir / "config.json"
        cfg.save(path)
        again = Config.load(path)
        assert again.room_id == "12345"
        assert again.volume == 55
        assert again.local_dirs == ["D:/music"]

    def test_load_missing_returns_default(self, data_dir):
        cfg = Config.load(data_dir / "nope.json")
        assert cfg.room_id == "" and cfg.volume == 20

    def test_load_corrupt_returns_default(self, data_dir):
        path = data_dir / "config.json"
        path.write_text("{not json", encoding="utf-8")
        assert Config.load(path).volume == 20

    def test_volume_clamped(self):
        assert Config(volume=500).normalized().volume == 100
        assert Config(volume=-3).normalized().volume == 0

    def test_room_id_stripped(self):
        assert Config(room_id="  12345 ").normalized().room_id == "12345"

    def test_unknown_keys_ignored(self, data_dir):
        path = data_dir / "config.json"
        path.write_text(json.dumps({"volume": 30, "未来字段": 1}), encoding="utf-8")
        assert Config.load(path).volume == 30

    def test_atomic_save(self, data_dir):
        path = data_dir / "config.json"
        cfg = Config(volume=1)
        cfg.save(path)
        for value in range(2, 30):
            Config(volume=value).save(path)
            assert json.loads(path.read_text(encoding="utf-8"))["volume"] == value
        assert [n for n in os.listdir(data_dir) if n.startswith(".tmp")] == []


# ---------------------------------------------------------------- 路径

class TestPaths:
    def test_data_dir_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DIANGE_DATA", str(tmp_path / "custom"))
        assert str(paths.data_dir()).endswith("custom")

    def test_resource_dirs_exist(self):
        assert paths.template_dir().is_dir()
        assert paths.image_dir().is_dir()
        assert paths.button_image_dir().is_dir()

    def test_templates_present(self):
        assert (paths.template_dir() / "index.html").is_file()
        assert (paths.template_dir() / "overlay.html").is_file()

    def test_images_present(self):
        assert (paths.image_dir() / "bg.png").is_file()
        assert (paths.image_dir() / "hero_web.png").is_file()
        assert len(list(paths.button_image_dir().glob("btn_*.png"))) > 0

    def test_ensure_dir(self, tmp_path):
        target = tmp_path / "a" / "b"
        assert paths.ensure_dir(target) == target
        assert target.is_dir()


# ---------------------------------------------------------------- 文本 / 时间

class TestText:
    @pytest.mark.parametrize("raw,expected", [
        ("点歌 晴天", "晴天"),
        ("点歌：晴天", "晴天"),
        ("点播 晴天", "晴天"),
        ("来一首晴天", "晴天"),
        ("想听 晴天", "晴天"),
        ("晴天", "晴天"),
    ])
    def test_strip_prefix(self, raw, expected):
        assert text_utils.strip_order_prefix(raw) == expected

    def test_ellipsize(self):
        assert text_utils.ellipsize("abcdef", 4) == "abcd…"
        assert text_utils.ellipsize("abc", 10) == "abc"

    def test_clean_filename(self):
        cleaned = text_utils.clean_filename('a/b\\c:d*e?f"g<h>i|j')
        for bad in '/\\:*?"<>|':
            assert bad not in cleaned

    def test_clean_filename_reserved(self):
        assert text_utils.clean_filename("CON") != "CON"

    def test_clean_filename_empty(self):
        assert text_utils.clean_filename("") != ""

    def test_escape_js_string_closes_tag(self):
        """内嵌进 ``<script>`` 时必须切断 ``</script>``。"""
        assert "</script>" not in text_utils.escape_js_string("</script>")

    def test_humanize_seconds(self):
        assert text_utils.humanize_seconds(0) == "0:00"
        assert text_utils.humanize_seconds(65) == "1:05"
        assert text_utils.humanize_seconds(-1) == "0:00"


class TestTimeutil:
    def test_now_ts_monotonic(self):
        assert timeutil.now_ts() <= timeutil.now_ts()

    def test_to_clock(self):
        clock = timeutil.to_clock(0)
        assert len(clock.split(":")) == 3


# ---------------------------------------------------------------- HTTP 工具

class TestWebHttp:
    def test_with_query(self):
        assert with_query("http://a/b", {"x": 1}) == "http://a/b?x=1"
        assert with_query("http://a/b?y=2", {"x": 1}) == "http://a/b?y=2&x=1"

    def test_with_query_encodes(self):
        assert "q=%E5%8F%B6" in with_query("http://a/b", {"q": "叶"})

    def test_host_of(self):
        assert host_of("https://y.qq.com/x") == "y.qq.com"
        assert host_of("http://127.0.0.1:8770/") == "127.0.0.1"

    def test_host_of_bad(self):
        assert host_of("not a url") == ""


# ---------------------------------------------------------------- 常量

class TestConstants:
    def test_order_prefixes_non_empty(self):
        from diangeji.constants import ORDER_PREFIXES
        assert ORDER_PREFIXES and all(ORDER_PREFIXES)

    def test_order_prefixes_match_text_module(self):
        """``constants.ORDER_PREFIXES`` 和 ``utils/text.py`` 的正则必须一致。

        ``utils`` 层不许依赖 ``constants``（分层规则），所以触发词只能存两份。
        那就在测试里把两份钉在一起：改了一处忘了另一处会立刻红。
        之前的注释谎称「避免循环导入」，没有任何机制保证一致性。
        """
        from diangeji.constants import ORDER_PREFIXES
        from diangeji.utils.text import strip_order_prefix
        for prefix in ORDER_PREFIXES:
            assert strip_order_prefix(f"{prefix} 晴天") == "晴天", \
                f"text.py 不认识 constants 里的触发词「{prefix}」"
        # 反过来：不该被当成点歌的句子
        assert strip_order_prefix("这歌真好听") == "这歌真好听"

    def test_audio_extensions_cover_mime(self):
        from diangeji.constants import AUDIO_EXTENSIONS, AUDIO_MIME
        assert set(AUDIO_EXTENSIONS) == set(AUDIO_MIME)

    def test_required_keys_defined(self):
        assert tuple(REQUIRED_KEYS[BILI_COOKIE]) == ("SESSDATA",)
        assert "qm_keyst" in REQUIRED_KEYS[QQ_COOKIE]


class TestLoggingSetup:
    """日志：重复 setup 不叠加 handler、windowed 模式不炸、控制台改 UTF-8。"""

    def test_setup_is_idempotent(self, tmp_path):
        from diangeji import logging_setup
        target = tmp_path / "a.log"
        logging_setup.setup(log_file=target, console=False)
        first = len(logging_setup.get_logger("x").handlers)
        logging_setup.setup(log_file=target, console=False)
        assert len(logging_setup.get_logger("x").handlers) == first

    def test_windowed_stderr_none_does_not_crash(self, tmp_path, monkeypatch):
        """--windowed 打包后 sys.stderr 是 None，写日志不能炸。"""
        from diangeji import logging_setup
        monkeypatch.setattr(sys, "stderr", None)
        logging_setup.setup(log_file=tmp_path / "b.log", console=True)
        logging_setup.get_logger("x").info("窗口模式下的一条日志")

    def test_console_stream_reconfigured_to_utf8(self):
        import io
        from diangeji import logging_setup
        stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        result = logging_setup._safe_stream(stream)
        assert result.encoding.lower().replace("-", "") == "utf8"

    def test_safe_stream_tolerates_stream_without_reconfigure(self):
        from diangeji import logging_setup

        class Plain:
            encoding = "gbk"

        stream = Plain()
        assert logging_setup._safe_stream(stream) is stream

    def test_log_file_is_utf8(self, tmp_path):
        from diangeji import logging_setup
        target = tmp_path / "c.log"
        logging_setup.setup(log_file=target, console=False)
        logging_setup.get_logger("x").info("中文日志测试")
        assert "中文日志测试" in target.read_text(encoding="utf-8")


class TestVersionConsistency:
    """版本号散在多处，改漏一处就是发布事故。"""

    def test_init_and_constants_agree(self):
        import diangeji
        from diangeji.constants import APP_VERSION
        assert diangeji.__version__ == APP_VERSION

    def test_pyproject_matches_package(self):
        import re
        from pathlib import Path
        import diangeji
        root = Path(__file__).resolve().parents[1]
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        assert match, "pyproject.toml 里找不到 version"
        assert match.group(1) == diangeji.__version__, \
            f"pyproject={match.group(1)} 但包内是 {diangeji.__version__}"

    def test_app_name_agrees(self):
        import diangeji
        from diangeji.constants import APP_NAME
        assert diangeji.__app_name__ == APP_NAME
