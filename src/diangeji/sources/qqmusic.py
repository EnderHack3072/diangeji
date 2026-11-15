"""QQ 音乐音源。

和原版比，改动集中在这几点：

* 直链缓存加了 **TTL + 命中日志**，且「拿不到 purl」也缓存一小会儿，
  避免 VIP 歌每次切歌都打一遍接口、把风控招来。
* 所有网络错误走 ``utils.webhttp``，拿不到数据就返回 ``None``，不抛。
* cookie 由 :class:`QQMusicConfig` 提供，不再直接读全局路径 —— 配置目录可注入，测试好写。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..constants import QQ_MUSIC_URL, UA_DESKTOP
from ..core.models import Track, TrackSource
from ..logging_setup import get_logger
from ..utils.webhttp import get_json, post_json
from .base import MusicSource

log = get_logger(__name__)

SEARCH_ENDPOINT = "https://u.y.qq.com/cgi-bin/musicu.fcg"
FAVORITE_LIST_ENDPOINT = "https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss"
FAVORITE_DETAIL_ENDPOINT = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"

#: 拿到的直链有效期（秒）。官方约 20 分钟，留 1 分钟余量。
URL_TTL = 19 * 60
#: 取不到直链时的负缓存，防止 VIP 歌反复打接口
MISS_TTL = 5 * 60


@dataclass
class QQMusicConfig:
    """QQ 音乐运行参数：cookie 文件位置 + 收藏缓存 TTL。"""

    data_dir: Path
    cookie_filename: str = "qq_cookie.txt"
    favorites_ttl: float = 600.0

    @property
    def cookie_path(self) -> Path:
        return self.data_dir / self.cookie_filename


class QQMusicSource(MusicSource):
    name = "QQ 音乐"
    source = TrackSource.QQ

    def __init__(self, config: QQMusicConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._url_cache: dict[str, tuple[str | None, float]] = {}
        self._fav_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])

    # ---------------- cookie ----------------

    def cookie(self) -> str:
        path = self.config.cookie_path
        try:
            return path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            return ""

    def enabled(self) -> bool:
        return bool(self.cookie())

    def invalidate(self) -> None:
        """清掉和登录态绑定的缓存（换号、重新扫码之后必须调）。

        ``cookie()`` 是实时读文件的，所以 ``enabled()`` 会立刻正确；
        但 ``_url_cache`` 里存的是**上一个登录态**拿到的直链，
        ``_fav_cache`` 是上一个号的红心歌单 —— 不清就会串号。
        """
        with self._lock:
            self._url_cache.clear()
            self._fav_cache = (0.0, [])

    def _uin(self, cookie: str | None = None) -> str:
        ck = self.cookie() if cookie is None else cookie
        for part in ck.split(";"):
            key, _, value = part.strip().partition("=")
            if key in ("uin", "p_uin"):
                return value.strip("oO ")
        return "0"

    def _headers(self) -> dict[str, str]:
        return {"Referer": QQ_MUSIC_URL, "User-Agent": UA_DESKTOP, "Cookie": self.cookie()}

    # ---------------- 搜索 ----------------

    def search(self, keyword: str, limit: int = 6) -> list[Track]:
        cookie = self.cookie()
        if not cookie or not keyword.strip():
            return []
        payload = {
            "comm": {"ct": 24, "cv": 0, "uin": self._uin(cookie)},
            "req_1": {
                "method": "DoSearchForQQMusicDesktop",
                "module": "music.search.SearchCgiService",
                "param": {"num_per_page": max(1, limit), "page_num": 1,
                          "query": keyword, "search_type": 0},
            },
        }
        data = post_json(SEARCH_ENDPOINT, payload, headers=self._headers(), timeout=12)
        try:
            songs = data["req_1"]["data"]["body"]["song"]["list"]  # type: ignore[index]
        except (KeyError, TypeError):
            return []
        return [self._parse_song(song) for song in songs or [] if song.get("mid")]

    @staticmethod
    def _parse_song(song: dict[str, Any]) -> Track:
        singers = "、".join(x.get("name", "") for x in (song.get("singer") or []))
        title = song.get("name") or song.get("title") or ""
        return Track(
            title=f"{title} - {singers}".strip(" -") if singers else str(title),
            mid=song.get("mid"),
            source=TrackSource.QQ,
            singer=singers,
            album=(song.get("album") or {}).get("name", ""),
        )

    def resolve(self, keyword: str) -> Track | None:
        hits = self.search(keyword, limit=1)
        if not hits:
            return None
        track = hits[0]
        return self.refresh(track)

    # ---------------- 直链 ----------------

    def stream_url(self, mid: str) -> str | None:
        """取直链，带正/负缓存。"""
        if not mid:
            return None
        now = time.time()
        with self._lock:
            cached = self._url_cache.get(mid)
            if cached is not None:
                url, expire_at = cached
                if expire_at > now:
                    return url
                self._url_cache.pop(mid, None)

        url = self._fetch_stream_url(mid)
        with self._lock:
            if len(self._url_cache) > 500:
                self._url_cache.clear()
            self._url_cache[mid] = (url, now + (URL_TTL if url else MISS_TTL))
        return url

    def _fetch_stream_url(self, mid: str) -> str | None:
        cookie = self.cookie()
        if not cookie:
            return None
        uin = self._uin(cookie)
        payload = {
            "comm": {"ct": 24, "cv": 0, "uin": uin},
            "req_1": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {"guid": "10000", "songmid": [mid], "songtype": [0],
                          "uin": uin, "loginflag": 1, "platform": "20"},
            },
        }
        data = post_json(SEARCH_ENDPOINT, payload, headers=self._headers(), timeout=12)
        try:
            body = data["req_1"]["data"]  # type: ignore[index]
            purl = body["midurlinfo"][0].get("purl")
        except (KeyError, IndexError, TypeError):
            return None
        if not purl:
            log.debug("QQ 音乐无可播地址（VIP/版权）：%s", mid)
            return None
        host = (body.get("sip") or ["https://dl.stream.qqmusic.qq.com/"])[0]
        return host.rstrip("/") + "/" + purl

    def refresh(self, track: Track) -> Track:
        """播放前重取直链；本地或没有 mid 的原样返回。"""
        if track.source is not TrackSource.QQ or not track.mid:
            return track
        url = self.stream_url(track.mid)
        return Track(title=track.title, path=url or track.path, mid=track.mid,
                     source=TrackSource.QQ, singer=track.singer, album=track.album)

    # ---------------- 收藏（空闲随机用） ----------------

    def favorites(self, limit: int = 200) -> list[dict[str, Any]]:
        """拉「我喜欢」歌单：``[{"title","singer","mid"}]``；失败返回空表。"""
        now = time.time()
        with self._lock:
            stamp, cached = self._fav_cache
            if cached and now - stamp < self.config.favorites_ttl:
                return list(cached)

        cookie = self.cookie()
        uin = self._uin(cookie)
        if not uin or uin == "0" or ("qm_keyst" not in cookie and "qqmusic_key" not in cookie):
            return []

        headers = self._headers()
        created = get_json(f"{FAVORITE_LIST_ENDPOINT}?hostuin={uin}&sin=0&size=200&format=json",
                           headers=headers, timeout=15)
        tid = self._favorite_dir_id(created)
        if not tid:
            return []
        detail_url = (
            f"{FAVORITE_DETAIL_ENDPOINT}?type=1&json=1&utf8=1&onlysong=0&disstid={tid}"
            f"&format=json&uin={uin}&g_tk=5381&loginUin={uin}&hostUin=0&inCharset=utf8"
            "&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0"
        )
        detail = get_json(detail_url, headers=headers, timeout=15)
        try:
            songs = (((detail or {}).get("cdlist") or [{}])[0]).get("songlist") or []
        except (AttributeError, IndexError, TypeError):
            return []
        out: list[dict[str, Any]] = []
        for song in songs[:limit]:
            mid = song.get("songmid")
            if not mid:
                continue
            out.append({
                "title": song.get("songname") or song.get("songorig") or mid,
                "singer": "、".join(x.get("name", "") for x in (song.get("singer") or [])),
                "mid": mid,
            })
        if out:
            with self._lock:
                self._fav_cache = (now, list(out))
        return out

    @staticmethod
    def _favorite_dir_id(created: Any) -> str | None:
        """从歌单列表里找「我喜欢」（dirid=201）的 tid。"""
        try:
            disslist = ((created or {}).get("data") or {}).get("disslist") or []
        except AttributeError:
            return None
        for item in disslist:
            if str(item.get("dirid")) == "201":
                return item.get("tid")
        return None
