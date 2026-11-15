# 变更记录

编号规则：一条一行号，只往后加，不回头改。

---

## 1 — 项目重构为 src-layout 分层结构

原来的平铺结构（`main.py` / `server.py` / `danmaku.py` / `wsclient.py` /
`qqmusic.py` / `login_qr.py` 全堆在根目录）重构为 `src/diangeji/` 下的分层包：

- `core/` 领域模型与状态机（`models` / `queue` / `library` / `errors`）
- `sources/` 音源适配（`base` / `local_source` / `qqmusic` / `registry`）
- `services/` 用例编排（`playback` / `library_service`）
- `live/` B 站弹幕（`protocol` / `wbi` / `commands` / `client`）
- `auth/` 登录（`store` / `qrcode_login` / `browser`）
- `web/` 本地 HTTP（`routes` / `security` / `views` / `server` / `templates` / `static`）
- `ui/` Tkinter 界面（`main_window` / `widgets`）
- `utils/` 工具（`ws` / `webhttp` / `text` / `timeutil`）

依赖严格单向：`ui`/`web` → `services` → `core`/`sources`/`live` → `utils`。
`app.py` 是唯一组合根。新增 `pyproject.toml`、`run.py`、`docs/architecture.md`。

## 2 — 图片资源移入包内

`bg.png` / `hero.png` / `hero_web.png` / `icon.ico` 移到
`src/diangeji/web/static/img/`，24 个按钮贴图移到 `web/static/img/buttons/`，
根目录不再有图片文件。新增 `paths.py` 作为所有路径的唯一出口。

## 3 — 修复「一首歌跳两首」

主播台（静音）和 OBS 覆盖层是两个页面，都监听 `<audio>` 的 `ended` 并各自
`POST /api/skip`，导致一首歌消耗掉两个播放位。

改为前端上报 `POST /api/ended {"id": "<曲目id>"}`，
`PlaybackService.skip_if_finished()` 只接受与当前播放项匹配的 id，
迟到的上报被丢弃。新增 `tests/test_web.py` 的并发上报回归测试。

## 4 — 修复 cookie 被写成空文件

原 `main.py` 里 `f.write(ck)` 的 `ck` 是 dict：`open(..., "w")` 先截断文件，
再抛 `TypeError`，把原本能用的 `qq_cookie.txt` 变成 0 字节。

`auth/store.py` 的 `CookieStore.write()` 改为：归一化 → 临时文件 → `os.replace`，
空内容直接拒绝。新增并发写入与「不留 .tmp 残file」测试。

## 5 — 联网移出锁外

原 `server.py` 的 `freshen()` 在 `STATE_LOCK` 内做网络请求，卡住整个服务。
`PlayQueue.advance(refresh=...)` 改为「锁内搬状态 → 锁外刷直链 → 锁内提交」，
锁外期间若有新点歌抢到播放位，候选曲目塞回队首。

## 6 — 修复 `/media/` 空路径返回第一首

原实现用 `endswith(rel)` 匹配文件，`rel` 为空时 `endswith("")` 恒真。
`LocalLibrary.path_for()` 改为精确匹配 + 拒绝空串。

## 7 — 修复 Range 请求 `bytes=-500` 语义

原实现把后缀 Range 当成 `0-500`。`_parse_range()` 重写，正确处理
`bytes=100-` / `bytes=100-200` / `bytes=-500` / 越界返回 416 / 多段降级。

## 8 — 修复 WBI 签名缺字符过滤

B 站 WBI 要求参数值过滤 `!'()*`。`WBISigner` 的 `_sanitize()` 补上，
签名结果与官方一致。新增测试覆盖参数重排、字符过滤、key 缓存。

## 9 — 加固本地服务同源校验

原 `_local_ok()` 在 `Origin` 和 `Referer` 都缺失时放行，等于对本机任意进程全开。
`web/security.py` 的 `is_local_request()` 改为**默认拒绝**：

- `Host` 必须是回环地址（挡 DNS rebinding）
- 有 `Origin`/`Referer` 时必须也是回环
- 两个都没有 → 拒绝，除非显式传 `allow_headless=True`

新增 `--allow-headless-api` 命令行开关和 `Application.allow_headless_api()`。

## 10 — 修复 Windows 端口占用检测失效

`ThreadingHTTPServer` 的 `allow_reuse_address = True` 在 Windows 上等价于
`SO_REUSEADDR`，允许**两个 socket 绑同一端口**，导致端口被占也检测不出来。
改为 Windows 下用 `SO_EXCLUSIVEADDRUSE`，POSIX 保持 `SO_REUSEADDR`。

## 11 — 修复 HTTP 服务线程崩溃

`HttpService._serve()` 的 `finally` 里读 `self._server.server_close()`，
而 `stop()` 会先把 `self._server` 置空，线程因此带着 `AttributeError` 死掉。
改为在方法开头抓本地引用。

## 12 — 修复空闲随机标记被清掉

`QueueItem.with_playback_started()` 原本把 `idle` 置为 `False`，
导致空闲随机播着的歌会一直占住播放位（观众的 `push()` 认不出该不该让位），
前端也显示不出「空闲随机」状态。改为保留 `idle` 标记。

## 13 — 修复 Tk 工作线程直接改控件

扫码轮询、启动装配、浏览器登录等后台线程原本直接操作 Tk 控件。
统一改为 `root.after(0, ...)` 回主线程执行。

## 14 — 启动不再无条件自动启动

原 `main.py` 用 `root.after(400, self.start)` 无条件拉起服务，
房间号没填时弹模态警告。改为只有 `config.auto_start` 为真且房间号非空才自动启动。

## 15 — 新增 `utils/ws.py` 的健壮性

WebSocket 客户端原来无帧长上限、缓冲区 O(n²) 追加、ping 递归处理。
改为 `bytearray` 缓冲 + `MAX_FRAME_BYTES` 上限 + 非递归 ping/pong。

## 16 — 新增 `clean_filename` 处理 Windows 保留设备名

`CON` / `PRN` / `AUX` / `NUL` / `COM1-9` / `LPT1-9` 不能当文件名，
即使带扩展名也不行。`utils/text.py` 的 `clean_filename()` 加前缀避开。

## 17 — 新增测试套件

`tests/` 下 324 项测试，覆盖：

- `test_core.py` 曲库匹配、队列状态机、模型
- `test_sources.py` 音源优先级、直链刷新、空闲随机
- `test_live.py` 弹幕协议边界、WBI 签名、点歌指令
- `test_web.py` 安全校验、模板转义、Range、真实 HTTP 往返
- `test_infra.py` cookie 原子写、配置读写、路径、文本工具
- `test_app.py` 组合根装配、HTTP 生命周期、模板契约、打包配置、点歌 worker

`test_app.py` 的模板契约测试会检查「两个页面是否都用带 id 的 `/api/ended`」，
作为双跳歌 bug 的回归守卫。

## 18 — 文档重写

`README.md` 按新结构重写，新增 `docs/architecture.md`（分层规则、线程模型、
数据流、新增功能该放哪），新增本文件。

## 19 — 前端按 id 而非 path 判断换歌

`index.html` / `overlay.html` 的 `syncAudio()` 原本用 `np.path !== cur` 判断
「是否换了首歌」。同一个人连点两次同一首**本地**歌时，前后两首 `path` 完全相同，
分支进不去 → `curId` 仍是上一首的、`endedSent` 仍是 `true` → 第二首的 `ended`
被丢弃，播放器停在结尾**永久不动**。

改为按 `np.id` 判断，并额外处理「id 不变但直链刷新了」的情况（QQ 音乐 vkey 过期）。
`test_regressions.py` 的 `TestPlayerSyncById` 钉住这一点，同时断言旧的
`want !== cur` 写法不会复活。

## 20 — 修 UI 线程里跑网络

`AppWindow._next()` / `_previous()` 原本在 Tk 回调里**同步**调
`playback.next()`，而它会刷新 QQ 音乐直链（联网，最坏 30 秒），队列空了还会
去取「我喜欢」—— 点一下「下一首」界面就冻住。

新增 `_guard_async()`：动作丢后台线程，按钮期间置灰，完成后经 `root.after(0, ...)`
回主线程。`QRLoginDialog` 的 `client.start()` 同样从构造函数里挪到后台线程，
先显示「正在获取二维码…」。

## 21 — 修 `WSClient` 握手失败漏 socket

`utils/ws.py` 的 `connect()` 把 `self.sock = raw` 放在握手**之前**，
`_read_header()` / `_verify_accept()` 抛异常时没人关闭 socket。
直播开一晚上重连几十次就漏几十个 fd。

改为 `try/except` 包住整个握手流程，失败时 `close(graceful=False)`。
`close()` 新增 `graceful` 参数 —— 握手失败时发 CLOSE 帧没有意义。

## 22 — 修 `-352` 只作废签名不重签

`live/client.py` 的 `_danmu_info()` 遇到 `-352` 风控时只做
`signer.invalidate()` 然后直接抛 `RateLimited`，白等 30 分钟冷却。
改为作废后**立刻重签重试一次**，仍失败才走缓存或冷却。

## 23 — 修 HTTP/1.1 keep-alive 的两个协议问题

- `do_POST` 校验失败时直接回 403 **没读 body**。HTTP/1.1 默认长连接，
  残留字节被当成下一条请求行 → 400 + 断连。新增 `_drain()`。
- `_media()` 自己拼响应，没有 HEAD 判断 → `HEAD /media/x` 也会发送整段音频 body。
  加 `if self.command == "HEAD": return`。

## 24 — 修 `CButton.set_text` 找不到贴图会让按钮消失

`ui/widgets.py` 的 `set_text()` 在贴图模式下若 `_load_images()` 失败，
`self.norm` 被置为 `None`，之后 hover 触发 `itemconfig(image=None)`，
按钮直接消失。改为找不到贴图就退回文字渲染。

## 25 — 消除 `ui → app` 反向依赖

`ui/main_window.py` 原本 `from ..app import Application`（运行时要真的导入），
并在 `launch()` 里 `from ..app import build_application` —— 表现层依赖装配层。

改为：`Application` 只在 `TYPE_CHECKING` 下导入（纯类型标注），
`launch(app, args=...)` 接收**调用方装配好的** app，装配统一由 `__main__.py` 做。
`app._new_queue()` 也有个函数内 import 绕循环的补丁，一并改成模块级导入。

## 26 — 新增分层守卫测试

`tests/test_layering.py` 用 AST 扫描把架构规则变成红线：向下依赖白名单、
没有模块 import `ui`、只有组合根能碰 `web`、`core` 不依赖上层、
`utils` 只许碰 `logging_setup`、包根不许出现业务模块、新增的层必须登记。
`if TYPE_CHECKING:` 里的导入不计入依赖。

## 27 — 新增回归守卫测试

`tests/test_regressions.py` 把外审发现的每个坑钉成测试：按 id 判断换歌、
HEAD 不带 body、403 后连接可继续、WebSocket 失败关 socket、
`with_playback_started()` 保留 idle 标记、根目录无旧入口、
`assert x or True` 这类假断言不许复活、CHANGELOG 编号连续且从 1 开始。

## 28 — 清理根目录旧入口

删除根目录残留的 `main.py` / `server.py` / `danmaku.py` / `wsclient.py` /
`qqmusic.py` / `login_qr.py` / `index.html` / `overlay.html`
（备份目录 `_备份_点歌姬_原始平铺结构_20261115` 里已有完整一份）。
根目录现在只剩 `run.py`、`pyproject.toml`、`README.md`、`CHANGELOG.md`、
`审查说明.txt`、`.gitignore`。

## 29 — 修复 Windows 端口占用与 HTTP 线程崩溃

- `ThreadingHTTPServer.allow_reuse_address = True` 在 Windows 等价于
  `SO_REUSEADDR`，允许两个 socket 绑同一端口 → 端口被占也检测不出来。
  改为 Windows 用 `SO_EXCLUSIVEADDRUSE`，POSIX 保持 `SO_REUSEADDR`。
- `HttpService._serve()` 的 `finally` 读 `self._server.server_close()`，
  而 `stop()` 会先把 `self._server` 置空 → 线程带 `AttributeError` 死掉。
  改为方法开头抓本地引用。

## 30 — 测试从 182 项扩到 303 项

新增 `test_sources.py`（40）、`test_app.py`（45）、`test_layering.py`（13）、
`test_regressions.py`（39），并修正了若干与真实 API 不符的测试假设。

## 31 — 修复控制台中文日志乱码

Windows 控制台默认 GBK 代码页，`StreamHandler(sys.stderr)` 打中文日志出来是
乱码，某些代码页下还会直接抛 `UnicodeEncodeError` 把日志打挂。
新增 `_safe_stream()`：尽量 `reconfigure(encoding="utf-8")`，换不了就保留原流。
文件日志一直是 UTF-8，不受影响。

## 32 — 验证打包链路

`python -m pip wheel .` 产出 `diangeji-1.1.0-py3-none-any.whl`（985 KB），
包内包含 2 个模板、28 张图片、`entry_points.txt`。装进干净 venv 后
`python -m diangeji --headless` 能正常起服务，`paths.template_dir()` 和
`image_dir()` 都能在安装位置找到资源。

## 33 — 弹幕收包线程不再被点歌解析阻塞

`Application._handle_order()` 原本在**弹幕收包线程**里同步走完整解析链路：
搜 QQ 音乐 → 取 vkey 直链 → 入队。一次慢查询最坏 24 秒，而弹幕长连接的
心跳窗口只有 25 秒 —— 直播间人多时点歌会串行排队，还随时可能把连接拖到
超时重连。

改为投递 + 消费：`_handle_order()` 只往 `queue.Queue` 里丢一条就返回，
单 worker 线程（`order-worker`）按顺序消费，保证入队顺序与弹幕顺序一致。
`shutdown()` 会投哨兵、`join(timeout=2)` 后退出。

## 34 — 新增 worker 行为测试

`test_app.py` 新增 `TestOrderWorker`（5 项）：点歌立即返回不阻塞、多首点歌
保持顺序、解析失败仍入队、shutdown 停掉 worker、重复 shutdown 安全。

## 35 — 双跳歌彻底修死（check-then-act → CAS）

`playback.skip_if_finished()` 原本是「先读 `queue.playing` 比对 id，再调
`next()`」—— 两步之间没有锁，两个页面**同时**上报 ended 时可能各自通过校验
再各切一次。第一版修法只挡掉了「稳定跳两首」，窗口还在。

`PlayQueue` 新增 `advance_if_playing(item_id, refresh)`，把「比对 id」和
「占住播放位」放进同一把锁，返回 `(是否切了, 快照)`。
`skip_if_finished()` 改走它。`test_regressions.py::TestConcurrentAdvanceIsAtomic`
用 4 个线程 + Barrier 同时上报同一个 id，断言只有 1 个生效。

## 36 — `previous()` 不再吞掉并发点歌

`previous()` 锁外刷直链后重锁，**无条件** `self._playing = target` ——
这期间观众点歌刚顶上去的那首会被直接覆盖掉（`advance()` 有
`if self._playing is None` 保护，`previous()` 漏了）。

改为对齐 `advance()`：播放位已被占就把历史项退回历史并记日志。

## 37 — pip 安装时数据目录不再跟着 cwd 跑

`paths.base_dir()` 在「非 frozen 且非 src 布局」时返回 `Path.cwd()` ——
pip 装在 site-packages 的用户从 A 目录启动和从 B 目录启动会得到**两份独立数据**，
看起来就像配置和登录凭空丢了。

新增 `user_data_dir()`：Windows 走 `%LOCALAPPDATA%\点歌姬数据`，
macOS 走 `~/Library/Application Support/`，其他走 `$XDG_DATA_HOME`。
源码运行和打包 exe 的行为不变（仍然跟着程序目录走）。

## 38 — 403 不再静默

原版 `server.py` 对没有 Origin/Referer 的请求是**放行**的；新版默认拒绝。
挂过 QQ 机器人/脚本的用户升级后会静默 403，且日志只有一行泛泛的
「拒绝非本机请求」，看不出该怎么办。

- 403 响应体新增 `hint` 字段，直接写明要加 `--allow-headless-api`
- 日志区分「Origin 不是回环」和「完全没有浏览器头」两种情况，
  后者每次启动只喊一次，并明确给出解决办法

## 39 — 原子写统一走 `paths.atomic_write()`

`auth/store.py` 和 `config.py` 各写一份原子写，且都有同一个毛病：
失败时把 `.tmp` 留在数据目录里。`live/client.py` 的 `danmu_hosts.json`
干脆还是裸 `open(...,"w")`。

统一抽成 `paths.atomic_write()` / `paths.discard()`，三处共用，失败必清临时文件。

顺带修掉一个更严重的问题：原来临时文件固定叫 `xxx.tmp`，**多个线程同时写
同一个目标会互相踩**（Windows 上表现为 `PermissionError` / `WinError 32`，
抢到的那个 `os.replace` 还可能换上半截内容）。现在文件名带 pid + 线程 id +
随机后缀，替换这一步在 Windows 上带退避重试。

`tests/test_infra.py::TestCookieStore::test_concurrent_writes_do_not_corrupt`
原本就是因为这个问题偶发失败，现在稳定通过。

## 40 — 退出流程：日志可见 + 不卡界面 + 给页面收尾时间

`AppWindow.quit()` 原本先 `self._closed = True` 再 `self.log("正在退出…")` ——
日志函数看到 `_closed` 就跳过，这行永远不会显示；而且 `shutdown()` 在 Tk
回调里同步跑，会停 HTTP、join 线程，窗口白住一下。

- 用独立的 `_closing` 标志防重复点击，`_closed` 留给真正的窗口销毁
- `shutdown()` 丢后台线程，完成后 `root.after(0, destroy)`
- 恢复了旧版那段宽限：页面还在轮询（`queue.last_seen_within(10)`）就等
  3.6 秒再关，免得 OBS 那一层瞬间黑屏

`last_seen_within()` 之前全仓库没调用，现在接上了。

## 41 — 测试扩到 314 项

`test_regressions.py` 从 18 项加到 29 项，新增：CAS 并发切歌只有一次生效、
`previous()` 不吞新点歌、原子写失败不留 `.tmp`、8 线程并发写同一目标、
数据目录不跟 cwd、403 响应带提示。

## 42 — 修掉第 37 条引入的双层数据目录

第 37 条改 `base_dir()` 时图省事让它直接返回「用户数据目录」，
而 `data_dir()` 本来就会再拼一层 `点歌姬数据` —— 结果 pip 安装下变成
`%LOCALAPPDATA%\点歌姬数据\点歌姬数据`。

`base_dir()` 现在返回的是**父目录**（`%LOCALAPPDATA%` 本身），
目录名统一由 `data_dir()` 拼。三种形态实测：
frozen → `exe目录\点歌姬数据`，源码 → `项目根\点歌姬数据`，
pip → `%LOCALAPPDATA%\点歌姬数据`，都不再嵌套。

原来的测试只断言了「不等于 cwd」，没看结果形状，所以没拦住 ——
补了一条 `test_installed_data_dir_has_no_double_nesting`。

## 43 — 补齐各模块详细文档（docs/modules/）

原来只有一份 `architecture.md` 讲整体分层，**每个子包具体怎么用没写** ——
函数签名、实现要点、踩过的坑全靠读代码。

新增 `docs/modules/` 下 9 份文档：

| 文件 | 内容 |
|---|---|
| `README.md` | 索引 + 三条共同约定 |
| `infra.md` | 路径三形态表格、`atomic_write()` 为什么要随机后缀 |
| `core.md` | 队列的锁外刷新三段式、CAS 为什么单独存在、快照结构 |
| `sources.md` | `MusicSource` 四个方法、「加一个音源」的完整步骤 |
| `services.md` | `skip_if_finished()` 双跳歌修复的来龙去脉 |
| `live.md` | 协议包头表、WBI `invalidate()` 的用途、5 步排错顺序 |
| `auth.md` | 三条登录路径、`write()` 为什么拒绝空内容 |
| `web.md` | `_drain()` 先读体的原因、Windows 端口复用陷阱 |
| `ui.md` | `launch()` 为什么必收 app、`quit()` 三处修复 |
| `utils.md` | WS 客户端为什么自己写、`close(graceful=False)` 的意义 |

所有签名都用 AST 从源码里提取后核对过，不是凭印象写的
（第一版把 `Config` 写成 `room`/`idle`/`dirs`，实际是
`room_id`/`idle_enabled`/`local_dirs`，已订正）。

配套测试：每个子包文档必须存在且超过 800 字符（防占位），
索引里的链接必须都能打开 —— 加测试时当场抓到一条指向不存在的
`architecture.md` 的链接，已修成 `../architecture.md`。

## 44 — 测试扩到 324 项

`test_regressions.py` 29 → 39：9 份模块文档的存在性与内容厚度、
索引链接可达性。

