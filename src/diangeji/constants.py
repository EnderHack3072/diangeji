"""全局常量：网络身份、端口、格式白名单等。业务规则不放在这里。"""

from __future__ import annotations

APP_NAME = "点歌姬"
APP_VERSION = "1.1.0"

#: 本地 HTTP 服务端口（OBS 浏览器源也指向这里）
DEFAULT_PORT = 8770

#: 主播台页面默认静音：声音交给 OBS 那层，避免双份
CONSOLE_QUERY = "?mute=1"

#: 浏览器自动化调试端口
CDP_PORT_QQ = 9223

#: 可播放音频扩展名
AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac")

#: 扩展名 → MIME
AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
}

#: 小于该大小的音频文件当音效跳过（抖音提示音之类）
MIN_AUDIO_BYTES = 300 * 1024

#: 弹幕点歌触发词
ORDER_PREFIXES = ("点歌", "点播", "来一首", "想听")

#: 允许访问本地服务的 Host / Origin 主机名
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})

#: 状态里日志与历史的保留条数
LOG_KEEP = 40
HISTORY_KEEP = 30

#: 单次请求体上限
MAX_BODY_BYTES = 1_000_000

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
UA_EDGE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

BILI_LIVE_URL = "https://live.bilibili.com/"
QQ_MUSIC_URL = "https://y.qq.com/"
