# `sources/` —— 音源层

`base.py` · `local_source.py` · `qqmusic.py` · `registry.py`

把「去哪儿找歌」抽象成一个接口，`services/` 只跟接口打交道，
不关心背后是本地文件还是 QQ 音乐。

---

## 接口（`base.py`）

```python
@dataclass
class ResolveResult:
    track: Track | None
    source: TrackSource
    detail: str = ""            # 失败原因，给日志用
    @property ok -> bool
    @classmethod miss(source, detail="") -> ResolveResult

class MusicSource(Protocol):
    def enabled() -> bool                        # 有没有条件用（登录态/目录）
    def search(keyword, limit=6) -> list[dict]
    def resolve(keyword) -> ResolveResult        # 搜 + 定首 + 取直链
    def refresh(track) -> Track | None           # 重取直链（老链接过期时）
```

**新增一个音源 = 实现这四个方法 + 在 `registry.default_registry()` 里注册。**
不需要改 `services/` 和 `core/`。

`refresh()` 返回 `None` 表示「刷新不了，按原样播」，调用方会容忍。

---

## `local_source.py`

```python
class LocalSource(MusicSource):
    def __init__(self, library: LocalLibrary)
```

- `enabled()` 就是「曲库里有没有歌」。
- `resolve()` 走 `library.find()`，命中就 `Track(path=..., source=LOCAL)`。
- `refresh()` 恒返回 `None` —— 本地文件不会过期。

---

## `qqmusic.py`

```python
@dataclass
class QQMusicConfig:
    cookie_path: Path | None
    ...

class QQMusicSource(MusicSource):
    def search(keyword, limit=6) -> list[dict]     # musicu.fcg 搜歌
    def resolve(keyword) -> ResolveResult
    def stream_url(mid) -> str | None              # vkey 直链
    def refresh(track) -> Track | None
    def favorites(limit=200) -> list[dict]         # 收藏夹（空闲随机用）
```

要点：

- **搜索**用 `musicu.fcg` 批量接口；**取直链**用 vkey 接口，两条路。
- 直链**有时效**（几小时），所以每次切歌前 `refresh()` 一次。
- 没有 cookie 时 `enabled()` 返回 `False`，`resolve()` 直接返回
  `ResolveResult.miss(...)`，不会抛异常。
- 没装 `brotli` 也能跑 —— 请求头里就不会要 br 编码。
- `_uin()` 从 cookie 里捞 `uin`/`wxuin`，`_headers()` 拼 Referer/UA。
  这两处是 QQ 接口的硬性要求，别乱改。

---

## `registry.py`

```python
class SourceRegistry:
    def add(source)
    def get(source: TrackSource) -> MusicSource | None
    def enabled_sources() -> list[MusicSource]
    def resolve(keyword, *, prefer=None) -> ResolveResult
    def refresh(track) -> Track | None
    def search_all(keyword, limit=6) -> list[dict]

def default_registry(local, qq) -> SourceRegistry
```

`resolve()` 的顺序是**本地优先，再 QQ**：

1. 遍历 `enabled_sources()`
2. 第一个 `ok` 的立即返回
3. 全都不行就返回「最后那个失败原因」

所以「本地有歌就不联网」这个行为由这里保证，不要在别处再写一遍。

`search_all()` 会并发查所有音源并把结果合并去重，给 `/api/search` 用。

---

## 为什么这层不碰队列

音源只回答「这首歌是什么」，**入不入队、要不要跳过不播的**，都是
`services/playback.py` 和 `core/queue.py` 的事。保持这条边界，
测试时可以直接换一个假音源（`tests/conftest.py` 里的 `FakeSource`），
完全不联网。
