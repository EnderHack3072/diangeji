# `core/` —— 纯逻辑层

`models.py` · `queue.py` · `library.py` · `errors.py`

**不联网、不碰文件、不碰界面。** 这一层可以在任何线程上调用，
所有状态变更都由内部的 `RLock` 保护。

---

## `models.py` —— 数据模型

```python
class TrackSource(str, Enum):
    LOCAL / QQ / NONE
    .label -> str              # 给人看的中文名
    @classmethod of(value)     # 宽容解析：认枚举、认字符串、认 None

class PlaybackPhase(str, Enum):  ...

@dataclass(slots=True)
class Track:
    title: str
    path: str | None = None       # 本地文件相对路径
    mid: str | None = None        # QQ 音乐 mid
    source: TrackSource = NONE
    singer: str = ""
    album: str = ""
    @property playable -> bool
    def to_dict() -> dict

@dataclass(slots=True)
class QueueItem:
    keyword: str
    title: str
    user: str                     # 谁点的
    path: str | None
    source: TrackSource
    mid: str | None = None
    id: str                       # new_id() 生成
    added: int                    # 入队时间戳
    started: int                  # 开播时间戳（0 = 还没播）
    idle: bool                    # 是否空闲随机塞的
    @property playable
    def with_playback_started() -> QueueItem     # 标记「已开播」
    def with_path(path) -> QueueItem
    def to_dict() / from_dict() / from_track()
    def copy_as_detached(user, idle=True)        # 做成空闲随机项
```

要点：

- 全部 `slots=True` —— 属性写错名字会立刻报错，不会静默多出一个字段。
- `TrackSource.of()` 是**宽容入口**：从 JSON / cookie 读回来的值可能是
  字符串，别让类型细节漏到调用方。
- `playable` 是唯一的「能不能播」判据：本地要有 `path`，QQ 要有 `mid`。
  `path=None` 的项**照样入队**，前端会标「缺音源」—— 观众的请求不该被
  静默吞掉。

---

## `queue.py` —— 播放队列（本层最复杂的模块）

```python
class PlayQueue:
    volume: int                       # 0-100，非法值保持原样
    idle: bool

    # 读
    playing -> QueueItem | None
    size -> int
    log_entries -> list[dict]
    snapshot() -> dict                # 给前端的完整状态
    needs_idle() -> bool

    # 写
    push(item)                        # 入队
    advance(refresh=None) -> dict     # 下一首
    advance_if_playing(item_id, refresh=None) -> (bool, dict)   # CAS 切歌
    previous(refresh=None) -> dict    # 上一首
    start_now(item)                   # 立刻播（插播）
    remove(item_id)  clear()
    set_paused(value=None)  set_volume(value)  set_idle(value=None)
    note(text)  resolving()  touch()  last_seen_within(sec)
```

### 三个容易踩的点

**① `advance()` 的锁外刷新**

QQ 音乐的直链有时效，切歌前要刷一次 —— 这是网络调用，**不能持锁**。
所以 `advance()` 是「加锁取候选 → 放锁刷直链 → 再加锁占位」三段。
最后一段必须重新检查播放位是否被别人占了：

```python
with self._lock:
    if self._playing is None:      # 还空着，占
        self._playing = candidate
    elif candidate is not None:    # 有人抢了，塞回队首，别丢
        self._queue.insert(0, candidate)
```

`previous()` 原来漏了同样的检查，会在锁外那几百毫秒里把观众刚点的歌
覆盖掉（CHANGELOG 第 36 条已修）。

**② `advance_if_playing()` 为什么要单独存在**

直播间有**两个页面**（主播台静音 + OBS 覆盖层），都挂 `<audio>`，都会触发
`ended` 并上报 `/api/ended`。

```python
# 反例：两步走，两个页面同时上报会各切一次
current = queue.playing
if current and current.id == item_id:
    queue.advance()          # ← 中间这段别的线程也能进来

# 正例：比对和占位在同一把锁里
switched, snapshot = queue.advance_if_playing(item_id)
```

返回 `(是否真的切了, 快照)`，调用方据此决定要不要做后续动作。
`test_regressions.py` 用 4 个线程 + `Barrier` 同时上报，断言只有 1 个生效。

**③ `volume` 的非法值处理**

`set_volume("abc")` 不抛异常、也不清零，而是**保持原值**。
界面滑杆有可能传出奇怪的东西，报错会打断拖动。

### 状态快照的结构

```json
{
  "playing":   {...} | null,
  "queue":     [{...}],
  "history":   [{...}],
  "pending":   [...],
  "log":       [...],
  "paused":    false,
  "volume":    20,
  "idle":      true,
  "idle_last": 0
}
```

`queue.snapshot()` 只有上面这几个键。**`lib`（曲库数量）不在这里** ——
它由 `web/views.py` 在渲染模板时另外注入。

前端两个页面都靠这个结构渲染，**改字段名要同时改两份模板**。

---

## `library.py` —— 本地曲库

```python
def title_of(path) -> str              # 从文件名猜「歌手 - 歌名」

class LocalLibrary:
    def scan(dirs, files=(), recursive=True)   # 扫描，累加
    def search(keyword="", limit=20) -> list[dict]
    def find(keyword) -> dict | None           # 单条
    def path_for(rel) -> Path                  # 相对路径 → 绝对路径
    def entries() / count() / sample(limit)
    def as_legacy_list() -> ...
```

- `search()` 返回**字典**（给 HTTP 层的 JSON 用），不是 `Track`。
  写测试时注意 `app.library.search("晴天")[0]["title"]`。
- `scan()` 是**累加**的，重复扫描不会清空已有条目。
- `path_for()` 是 `/media/<rel>` 的落点，必须保证结果在扫描根目录之内
  （别让 `../` 逃出去）。
- `__len__` / `__iter__` 都实现了，可以直接 `len(lib)`。

---

## `errors.py` —— 异常

```python
DianGeJiError(Exception)                 # 基类
├── EmptyKeywordError                    # 空关键词
├── TrackNotFoundError                   # 找不到
├── NoPlayableTrackError                 # 找到但播不了
├── LibraryUnavailableError              # 曲库不可用
└── DuplicateTrackError                  # 重复点歌
```

HTTP 层只捕获 `DianGeJiError` 并转成 400 + `{"ok": false, "error": "..."}`；
其他异常一律 500 且不把栈泄给浏览器。所以**业务异常要继承它**，
才能在界面上变成一句人话而不是「服务器内部错误」。
