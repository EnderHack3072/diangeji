# `services/` —— 应用服务层

`playback.py` · `library_service.py`

夹在 `core`/`sources` 和 `ui`/`web` 之间：**把多个底层能力拼成一件用户能感知的事**。
这一层不碰 Tkinter、不碰 HTTP，但允许联网（通过 `sources`）。

---

## `playback.py` —— 点歌与播放控制

```python
class PlaybackService:
    def __init__(queue, registry, idle_pool=(), favorites=None, rng=None)

    # 点歌
    def request(user, keyword) -> QueueItem

    # 切歌
    def next() -> dict
    def previous() -> dict
    def skip_if_finished(item_id) -> bool        # 播放器上报「唱完了」

    # 控制
    def set_paused(value=None)
    def set_volume(value)
    def remove(item_id)  clear()
    def set_idle(value=None)  set_idle_pool(pool)
    def snapshot() -> dict
```

### `request()` —— 点歌

```python
with self.queue.resolving():          # 标记「正在解析」，界面显示转圈
    result = self.registry.resolve(key)
track = result.track or Track(title=key, source=TrackSource.NONE)
item = QueueItem.from_track(track, keyword=key, user=user)
self.queue.push(item)
return item
```

两个刻意的设计：

- **解析在锁外** —— `registry.resolve()` 会联网，绝不能持队列锁。
- **解析失败也入队** —— `path=None` 的项照样进队列，前端标「缺音源」。
  观众的请求不该被静默吞掉，主播看得见才知道该去补歌。

### `skip_if_finished()` —— 双跳歌的修复点

```python
def skip_if_finished(self, item_id: str) -> bool:
    switched, _ = self.queue.advance_if_playing(item_id,
                                                refresh=self._refresh_item)
    if not switched:
        return False
    self._maybe_idle()
    return True
```

**这一行是整个项目的关键修复。** 原来的写法是

```python
current = self.queue.playing
if current is None or current.id != item_id: return False
self.next()          # ← 两步之间没有锁
```

主播台和 OBS 覆盖层是两个页面，都挂 `<audio>`，静音的那个也会触发 `ended`。
两个页面**同时**上报时，两边都能通过 id 校验，然后各切一次 —— 一首歌连跳两首。

`advance_if_playing()` 把「比对 id」和「占住播放位」放进同一把锁，
后到的那个因为 id 已经变了直接被丢弃。返回 `(是否切了, 快照)`，
调用方拿这个布尔值判断，不要自己再去读 `queue.playing` 比较。

### `_refresh_item()` —— 直链刷新

切歌前会调一次，走 `registry.refresh(track)`。失败返回原对象，
**不会阻塞切歌**（日志里记一条 warning）。

### 空闲随机

```python
_maybe_idle()          # 队列空了且 idle 打开时，随机塞一首
_pick_idle_item(picks) # 锁外选歌：这里会联网，调用方必须已放锁
_pool()                # 自定义池优先，没有就用收藏夹
```

自定义池来自 `点歌姬数据/idle_pool.txt`，每行「歌手 歌名」，`#` 开头跳过。

**`_idle_lock` 只表示「这一轮由我负责补歌」，不保护网络请求。**

早先 `_maybe_idle()` 把整个补歌流程包在锁里，而里头会
`registry.resolve()` 联网、失败还重试 `IDLE_RETRIES = 8` 次 ——
于是 `POST /api/skip` 最坏要等 8 × 15 秒，主播点「下一首」就卡住。
这也违反 `docs/architecture.md` 的铁律「持锁时不联网」。

现在的顺序是：

```python
if not self._idle_lock.acquire(blocking=False):
    return                      # 已经有人在补了
self._idle_lock.release()       # ★ 令牌到手就把锁放掉

item = self._pick_idle_item(picks)      # 联网，此时没有任何 idle 锁
with self.queue.resolving():
    self.queue.start_now(item)          # 提交，很快，不碰网络
```

`tests/test_regressions.py::TestIdleDoesNotHoldLockWhileNetworking`
用探针 registry 钉住这条：解析期间只要 `_idle_lock` 还被占着就红。
（第一版改法是「抢锁 → 联网 → finally 释放」，测试当场抓出来
联网时锁仍被持有 —— 锁的释放必须发生在联网**之前**。）

---

## `library_service.py` —— 曲库扫描

```python
class LibraryService:
    def __init__(library: LocalLibrary, data_dir: Path)
    def scan(extra_dirs=()) -> dict            # 扫描并返回概要

def default_music_dirs(data_dir: Path) -> list[str]
```

- `default_music_dirs()` 给出默认扫描位置：数据目录下的 `音乐/`、
  系统音乐文件夹（`~/Music`）。
- `scan()` 的 `extra_dirs` 来自配置里的 `dirs`，去重后一起扫。
- 扫描是**累加**的，重复扫不会丢已有条目。
- 返回的概要 `{"count": n, "samples": [...]}` 直接进 `/api/library`。

`_dedupe()` 负责路径归一化 + 去重 —— Windows 上大小写不同、`\` 和 `/`
混用都算同一个目录。

---

## 这一层的边界

| 该做 | 不该做 |
|---|---|
| 组合 core + sources 完成一件事 | 直接 `tkinter.xxx` |
| 联网（经 `sources`） | 直接 `http.server` |
| 返回 `dict` 快照给上层 | 决定 HTML 长什么样 |
| 用日志说明失败原因 | 弹窗 / 返回 HTTP 状态码 |

上层（`ui` / `web`）只调这一层，**不直接碰 `queue` 和 `registry`** ——
除了 `web/routes.py` 里读快照那一处（只读，无副作用）。
