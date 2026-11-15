# 点歌姬

B 站直播间弹幕点歌机：观众发「点歌 歌名」→ 进队列 → 自动播 → OBS 浏览器源出声。

主播端是一个 Tkinter 窗口，服务是一个只监听 `127.0.0.1` 的本地 HTTP 服务。
没有任何云服务，没有账号系统，数据全在自己机器上。

---

## 快速开始

### 直接跑源码

```bash
python run.py
```

`run.py` 会把 `src/` 加进 `sys.path`，不需要先 `pip install`。

也可以装成命令：

```bash
pip install -e .
diangeji              # 开界面
diangeji --headless   # 只跑服务，不开界面
```

### 环境要求

- Python 3.10+
- 运行时**只用标准库**
- 可选依赖（缺了也能跑，只是对应功能不可用）：
  - `qrcode` / `pypng` —— 扫码登录（`pip install -e ".[qr]"`）
  - `brotli` —— B 站弹幕 protover 3（`pip install -e ".[brotli]"`）

### 首次使用

1. 填直播间房间号（纯数字）
2. 点「① 扫码登录 B 站」——不收弹幕就收不到点歌
3. 点「② 扫码登录 QQ 音乐」——不登录就只有本地曲库
4. 点「④ 启动」

启动后：

- **主播台** `http://127.0.0.1:8770/?mute=1` —— 看队列、切歌、调音量。这页静音。
- **OBS 覆盖层** `http://127.0.0.1:8770/overlay` —— 加到 OBS 的「浏览器」源里，**声音从这里出**。

> OBS 里记得勾「关闭源时关闭页面」，宽高按你的画面给。
> 别在场景里再加一路音频采集，会双声道。

---

## 功能

| 功能 | 说明 |
|---|---|
| 弹幕点歌 | 识别「点歌 / 点播 / 来一首 / 想听」，同人同句 3 秒内只收一次 |
| 本地曲库 | 递归扫 mp3/flac/m4a/wav，小于 300KB 当音效跳过；本地优先，找不到才走 QQ 音乐 |
| QQ 音乐 | 扫码登录 / 浏览器登录 / 手动粘贴 cookie 三种方式；直链播放前刷新 |
| 空闲随机 | 队列空了自动放，优先 QQ 音乐「我喜欢」，可自定义歌单 |
| 队列管理 | 增删、切歌、上一首、清空、暂停、音量 |
| 全局快捷键 | `Ctrl+Alt+空格` 暂停/继续，`←/→` 切歌，`↑/↓` 音量 |

---

## 项目结构

```
点歌姬_源码_给审查/
├── run.py                  # 启动器（把 src/ 加进 sys.path）
├── pyproject.toml          # 打包配置
├── src/diangeji/
│   ├── __main__.py         # 命令行入口
│   ├── app.py              # 组合根：唯一知道各层怎么装配的地方
│   ├── paths.py            # 所有路径的唯一出口
│   ├── constants.py        # 常量
│   ├── config.py           # 配置读写（原子）
│   ├── logging_setup.py    # 日志
│   │
│   ├── core/               # 领域模型，不依赖任何东西
│   │   ├── models.py       #   Track / QueueItem / 枚举
│   │   ├── queue.py        #   PlayQueue 状态机
│   │   ├── library.py      #   LocalLibrary 曲库
│   │   └── errors.py       #   异常
│   │
│   ├── sources/            # 音源适配
│   │   ├── base.py         #   协议
│   │   ├── local_source.py #   本地
│   │   ├── qqmusic.py      #   QQ 音乐
│   │   └── registry.py     #   按优先级调度
│   │
│   ├── services/           # 用例编排
│   │   ├── playback.py     #   点歌 / 切歌 / 空闲随机
│   │   └── library_service.py
│   │
│   ├── live/               # B 站直播
│   │   ├── protocol.py     #   弹幕包格式
│   │   ├── wbi.py          #   签名
│   │   ├── commands.py     #   弹幕 → 点歌指令
│   │   └── client.py       #   WebSocket 客户端
│   │
│   ├── auth/               # 登录
│   │   ├── store.py        #   cookie 原子存取
│   │   ├── qrcode_login.py #   扫码（B站 / QQ音乐）
│   │   └── browser.py      #   浏览器登录（CDP）
│   │
│   ├── web/                # 本地 HTTP 服务
│   │   ├── routes.py       #   路由与处理器
│   │   ├── security.py     #   同源校验
│   │   ├── views.py        #   模板渲染
│   │   ├── server.py       #   生命周期
│   │   ├── templates/      #   index.html / overlay.html
│   │   └── static/img/     #   bg.png / hero.png / hero_web.png / icon.ico / buttons/
│   │
│   ├── ui/                 # Tkinter 界面
│   │   ├── main_window.py  #   主窗口 + 登录窗口
│   │   └── widgets.py      #   自绘控件
│   │
│   └── utils/              # 无业务含义的工具
│       ├── ws.py           #   WebSocket
│       ├── webhttp.py      #   HTTP
│       ├── text.py
│       └── timeutil.py
│
├── tests/                  # 377 项测试，pytest
└── docs/
    ├── architecture.md     # 分层约定与依赖规则
    └── modules/            # 各子包详细文档
        ├── README.md       #   索引 + 共同约定
        ├── infra.md        #   paths / config / constants / logging
        ├── core.md         #   模型、播放队列、曲库
        ├── sources.md      #   本地 / QQ 音乐 / 注册表
        ├── services.md     #   点歌解析、空闲随机
        ├── live.md         #   弹幕协议、WBI、指令、长连接
        ├── auth.md         #   cookie、扫码、CDP
        ├── web.md          #   路由、安全、模板
        └── ui.md           #   Tkinter 主窗口
```

### 分层规则

依赖**严格单向**，不允许反向 import，也不允许跨层直接调内部实现：

```
     ui  /  web           ← 表现层：只调服务，不写业务
          │
      services             ← 用例层：编排，不实现细节
          │
  core / sources / live    ← 领域层：模型、音源、协议
          │
       utils               ← 工具层：无业务含义
```

**没有任何模块 import `ui`**；`web` 不直接 import `sources` 的实现；
`ui` 也不 import `app`（装配好的实例由入口传进去）。

装配只发生在 `app.py`，它是唯一一处「知道谁住在哪」的代码。
想换掉某个实现（比如加网易云音源），改 `build_application()` 一处即可。

想看每个模块**具体怎么用**（函数签名、实现要点、踩过的坑），
进 [`docs/modules/`](docs/modules/README.md)。

这些规则由 `tests/test_layering.py` 用 AST 扫描强制执行，不是靠自觉。

细节见 `docs/architecture.md`。

---

## 曲库怎么放

默认扫这两个位置：

- `点歌姬数据/music/`
- `~/Music`

也可以在配置文件里加目录：

```json
{
  "local_dirs": ["D:\\音乐", "D:\\qq-bot\\music"]
}
```

配置在 `点歌姬数据/config.json`，改完重启。

---

## 数据目录

所有运行时数据在 **程序旁边的 `点歌姬数据/`**（绿色软件习惯，卸载直接删）。
**pip 安装**（没有「程序目录」可言）时改用用户级目录
`%LOCALAPPDATA%\点歌姬数据`，不跟着启动目录跑。

| 文件 | 内容 |
|---|---|
| `config.json` | 房间号、音量、扫描目录 |
| `bili_cookie.txt` | B 站登录态 |
| `qq_cookie.txt` | QQ 音乐登录态 |
| `idle_pool.txt` | 自定义空闲歌单（每行「歌手 歌名」，`#` 开头跳过） |
| `danmu_hosts.json` | 弹幕服务器缓存（自动重建，删了没影响） |
| `logs/diangeji.log` | 运行日志（2MB 轮转，留 3 份） |
| `_edgeprofile/` | 浏览器登录用的独立配置目录 |

可以用环境变量 `DIANGE_DATA` 改位置（打包版、源码运行、pip 安装都适用，
优先级最高）。

> Cookie 是**明文**存在这里的。发给别人请打包时排除这两个文件。

`config.json` 和 cookie 都是「先写临时文件再替换」，断电不会留半个文件。

---

## 接口

`/api/*` 只接受本机来源的请求（校验 `Host` + `Origin`/`Referer`），
且浏览器来的请求必须带 `Origin` 或 `Referer`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 主播台（注入初始状态） |
| GET | `/overlay` | OBS 覆盖层 |
| GET | `/api/state` | 完整状态快照 |
| GET | `/api/library` | 曲库概要 |
| GET | `/media/<相对路径>` | 音频流，支持 Range |
| GET | `/asset/<文件名>` | 图片（只允许 png/ico） |
| POST | `/api/order` | `{"user": "...", "text": "点歌 晴天"}` |
| POST | `/api/ended` | `{"id": "<曲目id>"}` 播放器上报播完 |
| POST | `/api/skip` 或 `/api/next` | 下一首 |
| POST | `/api/prev` | 上一首 |
| POST | `/api/pause` | 暂停/继续 |
| POST | `/api/volume` | `{"value": 0-100}` |
| POST | `/api/idle` | 开关空闲随机 |
| POST | `/api/remove` | `{"id": "..."}` |
| POST | `/api/clear` | 清空队列 |
| POST | `/api/search` | `{"q": "..."}` 搜曲库 |

### `/api/ended` 为什么带 id

主播台和 OBS 覆盖层是**两个页面**，都挂着播放器。主播台那页静音，
但静音的 `<audio>` 一样会触发 `ended`。如果两个页面都直接调 `/api/skip`，
一首歌会连跳两首 —— 这是原版的实际 bug。

现在前端上报 `{"id": 当前播放项的 id}`，服务端 `skip_if_finished()` 只认
**当前正在播的那一条**。后到的那个请求因为 id 已经变了会被丢弃。

「比对 id」和「切歌」在 `PlayQueue` 的同一把锁里完成（CAS，
`advance_if_playing()`），所以两个页面**同时**上报也只会切一次 —— 不会出现
「各自通过校验，然后各切一次」。

外接 QQ 机器人转发点歌：

```bash
curl -X POST http://127.0.0.1:8770/api/order \
     -H "Content-Type: application/json" \
     -d '{"user":"群友","text":"点歌 晴天"}'
```

脚本没有 `Origin` 头，**默认会被拒**（原版是放行的，这是升级后唯一会破坏
既有用法的改动）。启动时加 `--allow-headless-api` 放开。

被拒时返回的 403 会带一句提示，日志里也会写明该怎么改，不会静默失败：

```json
{"ok": false, "error": "forbidden",
 "hint": "本接口只接受本机浏览器发来的请求。脚本 / QQ 机器人 / 自动化工具请启动时加 --allow-headless-api"}
```

---

## 开发

```bash
pip install -e ".[dev]"
pytest                         # 377 项
pytest -q tests/test_core.py   # 单文件
```

打包成 exe：

```bash
python scripts/build.py
```

### 测试分布

| 文件 | 管什么 |
|---|---|
| `test_core.py` (26) | 队列状态机、曲库匹配、模型 |
| `test_sources.py` (40) | 音源优先级、直链刷新、空闲随机 |
| `test_live.py` (50) | 弹幕协议边界、WBI 签名、点歌指令 |
| `test_web.py` (53) | 安全校验、模板转义、Range、真实 HTTP 往返 |
| `test_infra.py` (58) | cookie 原子写、配置、路径、文本工具 |
| `test_app.py` (45) | 组合根装配、HTTP 生命周期、模板契约、打包配置、点歌 worker |
| `test_layering.py` (13) | **架构守卫**：AST 扫依赖方向，违反就红 |
| `test_regressions.py` (39) | **回归守卫**：修过的 bug 不许复活 |

后两个文件不测功能，测的是「代码库的整体性质」。
比如 `test_layering.py` 会断言「没有任何模块 import `ui`」「`core` 不依赖上层」；
`test_regressions.py` 里每一条都对应一个真实发生过的 bug。

---

## 已知限制

### 音乐源

- QQ 音乐没有官方接口，靠网页登录 cookie。过期后点歌全部「未找到」，需要重新登录。
- VIP / 版权受限曲目拿不到直链，会显示「缺音源」。
- 直链约 20 分钟过期：**每次切歌前都会重新取**，所以直播几小时没事；
  但单曲如果超过 20 分钟，播到一半会断，之后自动跳下一首。

### 运行

- 队列只在内存里，关掉程序就清空。
- 端口固定 `8770`，被占用时启动失败并提示。**不会**悄悄换端口。
- 弹幕断线按 3/6/12/30/60 秒退避重连；取不到 token 时冷却 5 分钟。

### 安全

- 只监听 `127.0.0.1`，别把 8770 转发到公网。
- 页面注入的状态做过 `<script>` 转义，观众发 `</script>` 不会被执行。
- 但仍然是**无鉴权的本地服务** —— 同机器上的进程可以调 API（这是设计如此）。

### 没做

- 点歌冷却、每人同时最多排几首、屏蔽词
- 播放进度条、淡入淡出
- 网易云 / 酷狗（接口不一样，得各写一个 source）

---

## 许可

个人项目，随便用。
