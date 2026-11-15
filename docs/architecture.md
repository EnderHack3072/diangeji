# 架构说明

这份文档说明**为什么这么分层**、**依赖往哪走**、**新增功能该放哪**。

> 想知道每个模块的**具体接口和实现细节**，看
> [`modules/`](modules/README.md) —— 那里按子包分文件写，
> 每个模块一份（`infra` / `core` / `sources` / `services` / `live` /
> `auth` / `web` / `ui` / `utils`）。
> 本文只讲整体形状。

---

## 一句话

`ui` / `web` 只负责「把用户的操作翻译成一次服务调用」，
`services` 只负责「按正确顺序调几个领域对象」，
`core` / `sources` / `live` 只负责「自己的领域内正确」，
`utils` 只负责「通用机制」。

**任何一层都可以在不知道上面一层存在的情况下写出来并被单测。**

---

## 依赖方向

```
┌───────────────────────────────────────────┐
│  ui/            web/                      │   表现层
│  Tkinter 窗口    本地 HTTP + 模板          │   只调 services，不写业务
└───────────────┬───────────────────────────┘
                │
┌───────────────▼───────────────────────────┐
│  services/                                │   用例层
│  playback / library_service               │   编排顺序，不做具体实现
└───────┬───────────────────┬───────────────┘
        │                   │
┌───────▼───────┐  ┌────────▼──────────┐
│  core/        │  │  sources/  live/  │   领域层
│  模型 + 状态机 │  │  音源      协议    │   自己的领域内自洽
└───────┬───────┘  └────────┬──────────┘
        │                   │
┌───────▼───────────────────▼───────────────┐
│  utils/                                   │   工具层
│  ws / webhttp / text / timeutil           │   没有业务含义
└───────────────────────────────────────────┘
```

**规则：箭头只能往下，不能往上，也不能横跳。**

具体的禁止项：

| 禁止 | 原因 |
|---|---|
| 任何模块 `import diangeji.ui` | 界面是最容易变的东西，不能被依赖 |
| `web/` import `sources/qqmusic.py` | 换音源不该动 HTTP 层 |
| `core/` import `sources/` 或 `services/` | 模型必须能独立测 |
| `utils/` import 业务模块（`logging_setup` 除外） | 工具层一旦有业务就变成第二层屎山 |
| `services/` import `web/` 或 `ui/` | 用例不知道谁在调它 |
| `ui/` import `app.py` | 表现层不能依赖装配层；app 由入口传进来 |

`app.py` 是唯一的例外 —— 它是**组合根**，所有层它都能碰，因为它就是来装配的。

**这些规则不是靠自觉**：`tests/test_layering.py` 用 AST 扫描源码，
每次跑 pytest 都会检查一遍。新增一层必须同时在 `ALLOWED` 表里登记，
否则测试直接失败。

---

## 各层职责与边界

### `core/` —— 领域模型

**放什么**：数据结构、状态机、纯逻辑判断。

- `models.py`：`Track`、`QueueItem`、`TrackSource`
- `queue.py`：`PlayQueue`，播放队列的全部状态转换
- `library.py`：`LocalLibrary`，本地文件扫描与匹配
- `errors.py`：异常类型

**不放什么**：网络、文件 IO（除了曲库扫描必须的文件系统）、配置、日志格式化。

`PlayQueue` 是这块的核心：它持有锁、维护 `playing / queue / history` 三个位置，
所有状态变更都通过它的方法。**它不做网络请求** —— 需要刷新直链时，
调用方传一个 `refresh` 回调进来，`advance()` 会在**锁外**调用它。

### `sources/` —— 音源

**放什么**：怎么从某个地方拿到一首能播的歌。

- `base.py`：`MusicSource` 协议 + `ResolveResult`
- `local_source.py`：本地曲库
- `qqmusic.py`：QQ 音乐（登录态、搜索、取 vkey 直链）
- `registry.py`：按优先级依次尝试，`enabled()` 为假的直接跳过

新增一个音源（比如网易云）只需要：

1. 写一个类实现 `MusicSource` 的五个成员
2. 在 `app.build_application()` 里 `registry.add(...)`

**不用动** `services`、`web`、`ui`。

### `live/` —— B 站直播

**放什么**：弹幕协议、签名、指令解析、长连接客户端。

- `protocol.py`：16 字节包头 + zlib/brotli 嵌套包
- `wbi.py`：WBI 签名（含 `!'()*` 过滤）
- `commands.py`：`"点歌 晴天"` → `Order(keyword="晴天")`
- `client.py`：`DanmakuClient`，重连、心跳、去重、状态上报

`client.py` 拿到点歌指令后**不直接操作队列**，而是回调 `on_order(user, order)`。
回调是 `app.py` 在装配时注入的，指向 `playback.request()`。

### `services/` —— 用例

**放什么**：一次完整的用户操作该按什么顺序发生。

- `playback.py`：
  - `request()` —— 解析（锁外）→ 入队（锁内）
  - `next()` / `previous()` —— 切歌，含锁外直链刷新
  - `skip_if_finished(id)` —— 播放器上报播完，**只认当前播放项**
  - 空闲随机：队列空了自动补歌，有人点歌时让位
- `library_service.py`：曲库扫描（默认目录 + 用户配置）

**不放什么**：HTTP 状态码、JSON 序列化、Tk 控件。

### `web/` —— 本地 HTTP

**放什么**：HTTP 到服务调用的翻译。

- `routes.py`：路由表 + `AppContext`（注入依赖的容器）
- `security.py`：`is_local_request()`，默认拒绝
- `views.py`：模板渲染与 `<script>` 上下文转义
- `server.py`：`HttpService`，起停线程

处理器不持有全局状态 —— 依赖全在 `AppContext` 里，测试时换成假的即可。

### `ui/` —— Tkinter

**放什么**：窗口、控件、事件绑定。

- `main_window.py`：主窗口 + 扫码登录窗口
- `widgets.py`：自绘控件（Canvas 圆角按钮、贴图按钮、音量滑块）

**约定**：

- 所有从后台线程来的界面更新必须走 `root.after(0, ...)`。
  Tk 不是线程安全的，直接从工作线程改控件在 Windows 上会偶发崩溃。
- **会联网的按钮一律走 `_guard_async()`**：`playback.next()` / `previous()`
  会刷新 QQ 直链（最坏 30 秒），在 Tk 回调里同步跑 = 界面冻死。
- `ui` **不 import `app.py`**。`Application` 只在 `TYPE_CHECKING` 下导入（纯标注），
  装配好的实例由 `__main__.py` 传进 `launch(app, args=...)`。

### `utils/` —— 工具

**放什么**：与业务无关的通用机制。

- `ws.py`：WebSocket 客户端（帧长上限、非递归 ping 处理）
- `webhttp.py`：HTTP 请求 + JSON 解析（含 GBK 兜底）
- `text.py`：字符串处理
- `timeutil.py`：时间格式

**检验标准**：把 `utils/` 原样搬到另一个项目，它应该还能用。

---

## 装配：`app.py`

`build_application()` 是唯一一处「知道具体类名」的地方：

```python
library = LocalLibrary()
library_service = LibraryService(library, data)
cookies = CookieStore(data)
qq_source = QQMusicSource(QQMusicConfig(data_dir=data))
registry = default_registry(LocalSource(library), qq_source)
playback = PlaybackService(queue=..., registry=registry, ...)
ctx = AppContext(playback=playback, library=library, ...)
http = HttpService(ctx=ctx, port=port)
app = Application(...)
ctx.danmaku_state = app.danmaku_state   # 打破 http → app 的反向依赖
```

注意最后一行：HTTP 层需要报告弹幕状态，但弹幕客户端在 `Application` 上。
与其让 `web` import `app`（反向依赖），不如**注入一个回调**。

测试时把这个回调换成 `lambda: {"connected": False}` 就够了，不需要真连弹幕。

---

## 线程模型

| 线程 | 谁起的 | 干什么 | 不能干什么 |
|---|---|---|---|
| 主线程 | `__main__` | Tk 事件循环、热键 | 不能做网络请求 |
| `http` 线程池 | `web/server.py` | 每请求一个线程，共享 `PlayQueue` | 不能直接改 Tk 控件 |
| `danmaku` | `live/client.py` | WebSocket 收包 + 心跳 | **不能做耗时解析** |
| `order-worker` | `app.py` | 顺序解析点歌（搜索 + 取直链） | 不能直接改 Tk 控件 |
| `qr-fetch` / `qr-poll` | `ui/main_window.py` | 取二维码、轮询扫码状态 | 不能直接改 Tk 控件 |
| `hotkeys` | `GetMessageW` | 全局快捷键 | 触发的动作走 `root.after` |

**为什么点歌要单独一个 worker**：解析一首歌要走 QQ 音乐搜索 + 取 vkey 直链，
最坏 24 秒；而弹幕连接的心跳窗口只有 25 秒。如果在收包线程里同步解析，
一次慢查询就能把长连接拖到超时重连 —— 直播间人多时还会串行排队。
`_handle_order()` 只投递，`order-worker` 单线程顺序消费，保证入队顺序正确。

**保护队列状态的是 `PlayQueue._lock`**，管 `playing / queue / history`。
另有两把**不保护业务状态**的辅助锁，别混为一谈：

| 锁 | 保护什么 | 允许持锁联网吗 |
|---|---|---|
| `PlayQueue._lock` | 队列状态本体 | **不行** |
| `PlaybackService._idle_lock` | 只表示「这一轮由我负责补歌」 | **不行**（抢到就立刻放） |
| `QQMusicSource._lock` | 直链/歌单缓存字典 | 不行 |

**铁律：持锁时不联网、不碰 Tk、不写文件。**

`_idle_lock` 曾经是反例：它把整个补歌流程（含 `registry.resolve()`
联网、失败重试 8 次）包在锁里，于是主播点「下一首」最坏要等
8 × 15 秒。现在它只在**抢令牌**那一刻被持有，抢到就释放 ——
它表达的是「谁去补这一首」，不是「我要一直占着直到联网完」。
（`tests/test_regressions.py::TestIdleDoesNotHoldLockWhileNetworking`
盯着这条。）

`PlayQueue.advance(refresh=...)` 就是为这条规则设计的：

```python
with self._lock:            # 锁内：只做状态搬移
    current = self._playing
    self._push_history_locked(current)
    candidate = self._pop_playable_locked()

if candidate is not None:
    candidate = refresh(candidate)      # 锁外：网络请求

with self._lock:            # 锁内：提交结果
    ...
```

锁外那段时间队列可能被别人改了，所以提交时要判断「播放位是不是已经被占了」，
占了就把 `candidate` 塞回队首（`advance()` 里的 `elif candidate is not None` 分支）。

---

## 数据流

### 一次弹幕点歌

```
B 站弹幕服务器
  → live/client.py 收包、解压、找 DANMU_MSG
  → live/commands.py parse_order("点歌 晴天") → Order("晴天")
  → on_order 回调
  → app.Application._handle_order()
  → services/playback.py request("观众", "晴天")
      ├─ sources/registry.py resolve("晴天")  ── 锁外，可能走网络
      │    ├─ local_source 命中？ 用本地文件
      │    └─ 没命中 → qqmusic 搜索 + 取 vkey
      └─ core/queue.py push(QueueItem)        ── 锁内
  → 界面和网页下次轮询 /api/state 时看到
```

### 一次播放结束

```
<audio> 触发 ended
  → 前端 POST /api/ended {"id": "a1b2c3d4"}
  → web/routes.py
  → services/playback.py skip_if_finished("a1b2c3d4")
      ├─ 比 current.id != item_id → 丢弃（另一个页面迟到的上报）
      └─ 相等 → next()
```

---

## 新增功能往哪放

| 你要做的事 | 放哪 | 要不要动别的层 |
|---|---|---|
| 加一个新音源 | `sources/` 新文件 | 只改 `app.py` 装配 |
| 加一个新 HTTP 接口 | `web/routes.py` 的 `_route_get` / `_route_post` | 业务逻辑放 `services/` |
| 改界面布局 | `ui/` | 不动 |
| 改播放队列规则 | `core/queue.py` | 加测试 |
| 改「点歌」触发词 | `constants.py` 的 `ORDER_PREFIXES` | 不动 |
| 加弹幕新指令（比如「切歌」） | `live/commands.py` + `client.py` 回调 | `app.py` 接回调 |
| 加配置项 | `config.py` 的 `Config` | 界面读 `app.config` |
| 改日志格式 | `logging_setup.py` | 不动 |
| 写任何文件 | `paths.atomic_write()` | 别自己拼 `.tmp` 名字 |
| 往数据目录加状态文件 | `paths.data_dir()` | 别用相对路径 |

### 两条容易踩的约定

**写文件一律 `paths.atomic_write()`。** 它保证断电不留半个文件，并且临时
文件名带 pid + 线程 id + 随机后缀 —— 固定叫 `x.tmp` 的话，两个线程同时写
同一个目标会互相踩（Windows 上直接 `PermissionError`）。它还在 Windows 上
对 `os.replace` 做了退避重试，因为覆盖一个正被打开的文件会短暂失败。

**要先比对再改状态，就用 CAS 形式。** `queue.advance()` 单纯切歌；
`queue.advance_if_playing(id)` 把「比对 id」和「占住播放位」放进同一把锁，
返回 `(是否切了, 快照)`。`playback.skip_if_finished()` 走的是后者 ——
分成两步（先读 id 再调 next）在两个页面同时上报时会各切一次。

---

## 测试策略

| 层 | 怎么测 | 依赖 |
|---|---|---|
| `core/` | 纯单测，造 `QueueItem` 直接调 | 无 |
| `sources/` | 假音源（`tests/conftest.py` 的 `FakeSource`） | 临时目录 |
| `services/` | 假音源 + 真队列 | 临时目录 |
| `live/` | 直接调 `pack()` / `parse_order()`，不连网 | 无 |
| `auth/` | 临时目录测原子写入、并发写 | 临时目录 |
| `web/` | 真的起 `ThreadingHTTPServer` 到端口 0，用 urllib 打 | 无 |
| `ui/` | **不测**（需要显示器） | — |

`web` 的测试会真起服务，这是故意的：路由、Range、安全校验、keep-alive
这些只有在真实 HTTP 往返里才测得出来。

### 四类测试，各管一件事

| 文件 | 管什么 | 数量 |
|---|---|---|
| `test_core.py` / `test_sources.py` | 领域逻辑对不对 | 66 |
| `test_live.py` / `test_web.py` | 协议与 HTTP 边界 | 103 |
| `test_infra.py` / `test_app.py` | 基础设施与装配 | 103 |
| `test_layering.py` | **架构规则没被破坏** | 13 |
| `test_regressions.py` | **修过的 bug 没有复活** | 39 |

后两类是「元测试」：它们不测功能，测的是「代码库的整体性质」。
`test_layering.py` 用 AST 扫依赖方向；`test_regressions.py` 里每一条都对应
一个真实发生过的 bug（按 id 判断换歌、HEAD 不发 body、403 后连接可用、
WebSocket 失败关 socket、假断言不许复活……）。

---

## 已知的设计取舍

| 取舍 | 为什么这么选 |
|---|---|
| 队列只在内存 | 直播场景不需要跨重启恢复；加持久化反而要处理「恢复时文件没了」 |
| 端口固定不换 | 悄悄换端口会让 OBS 源指向错误地址，比启动失败更难查 |
| `web` 不 import `app` | 用回调注入换掉反向依赖，代价是 `AppContext` 多一个字段 |
| `ui` 不 import `app` | app 由 `__main__` 装配后传进 `launch()`；代价是 `launch` 签名多一个参数 |
| 扫码用轮询不用长连接 | 登录是一次性操作，省一个常驻连接 |
| 浏览器登录用 CDP 不用 cookie 数据库解密 | Chromium 的加密随版本变，解密很脆；CDP 是官方调试接口 |
| UI 不测 | Tk 单测收益低、成本高；逻辑已经全部下沉到 `services` |
