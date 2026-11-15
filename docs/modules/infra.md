# 地基模块

`paths.py` · `config.py` · `constants.py` · `logging_setup.py`

这四个模块是**叶子**：谁都可以 import 它们，它们不 import 任何业务模块。

---

## `paths.py` —— 路径解析

三种运行形态都要能跑：源码运行、PyInstaller 打包、pip 安装。

```python
is_frozen() -> bool                       # 是否打包环境
package_root() -> Path                    # 含 web/ ui/ 的那一级
base_dir() -> Path                        # 数据目录的「父目录」
user_base_dir() -> Path                   # 平台用户目录
data_dir() -> Path                        # 真正的数据目录
```

### 数据目录怎么定

| 形态 | `base_dir()` | `data_dir()` |
|---|---|---|
| PyInstaller 打包 | exe 所在目录 | `exe目录/点歌姬数据` |
| 源码运行（`src/` 布局） | 项目根 | `项目根/点歌姬数据` |
| pip 安装到 site-packages | `%LOCALAPPDATA%`（Win） | `%LOCALAPPDATA%/点歌姬数据` |

优先级：`DIANGE_DATA` 环境变量 > 上面那张表。

**`base_dir()` 返回的是父目录，不是数据目录本身** —— 目录名 `点歌姬数据`
只由 `data_dir()` 拼一次。这条曾经写错过，结果 pip 安装下变成
`%LOCALAPPDATA%\点歌姬数据\点歌姬数据` 双层嵌套（CHANGELOG 第 42 条）。

> pip 安装的用户没有「程序目录」可言。如果退化用 `Path.cwd()`，从 A 目录
> 启动和从 B 目录启动会得到两份独立数据，看起来就像配置和登录凭空丢了。

### 写文件

```python
atomic_write(target: Path, text: str, *, encoding="utf-8") -> None
discard(path: Path) -> None
ensure_dir(path: Path) -> Path
```

`atomic_write()` 做三件事：

1. 先写临时文件（**文件名带 pid + 线程 id + 随机后缀**）
2. `os.replace()` 替换目标 —— 原子，断电不会留半个文件
3. 失败时删掉临时文件，不在数据目录里留垃圾

第 1 点里的随机后缀不是装饰：临时文件固定叫 `x.tmp` 的话，两个线程同时写
同一个目标会互相踩，Windows 上直接 `PermissionError`。第 2 点在 Windows
上还带退避重试（覆盖一个正被打开的文件会短暂失败，POSIX 不会）。

### 包内资源（只读）

```python
resource_dir(*parts)   template_dir()   static_dir()
image_dir()   button_image_dir()   icon_path()
```

这些指向 `src/diangeji/web/` 下面，**永远只读**。打包时由
`scripts/build.py` 一起塞进 wheel/exe。

---

## `config.py` —— 配置

```python
@dataclass
class Config:
    room_id: str          # 直播间房间号
    volume: int           # 音量 0-100
    idle_enabled: bool    # 空闲随机开关
    local_dirs: list[str] # 额外扫描目录
    auto_start: bool      # 启动即连弹幕
    def normalized(self) -> Config      # 校正越界值
    @classmethod load(cls, path=None) -> Config
    def save(self, path=None) -> Path
```

- `load()` 对**任何**异常都退回默认值（文件不存在、JSON 坏了、键多余）——
  配置文件坏了不该导致程序起不来。
- `save()` 走 `atomic_write()`。
- `normalized()` 负责把音量夹到 0-100、目录去重去空。**读取后一定调用它**，
  不要假设文件里的值合法。

---

## `constants.py` —— 常量

房间号 URL、UA、默认端口、`ORDER_PREFIXES`（点歌触发词）、各超时值、
`HISTORY_KEEP` / `LOG_KEEP` 等。

**没有逻辑，只有数据。** 改触发词就改这里，别在 `live/commands.py` 里硬编码。

### `LOCAL_HOSTS` —— 回环地址的唯一来源

```python
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
```

`web/security.py` 的 `LOOPBACK` 就是它的**别名**，不再自己补一份。
早先两边各存一份（`security` 额外并了 `{"::1", "[::1]"}`），
结果是「哪些 host 算本机」有两个真相，想放开某个地址得改两处。

`::1` 必须在里面：Windows 上 `localhost` 常先解析到 IPv6，
漏掉会让浏览器自己打开 `http://localhost:8770` 直接吃 403。
`[::1]` 是 `Host` 头里的形态（带方括号），两种都要有。

> `ORDER_PREFIXES` 在 `utils/text.py` 里有一份**等价的正则**。那不是循环导入
> 的规避（`constants` 零 import），而是分层规则：`utils` 不许依赖 `constants`。
> 代价是改触发词要动两处，所以 `tests/test_infra.py` 有一条测试把两者钉在一起。

---

## `logging_setup.py` —— 日志

```python
setup(level=INFO, log_file=None, console=True) -> None
get_logger(name="") -> logging.Logger
```

- **幂等**：重复调用不会挂两套 handler（测试里有验证）。
- 日志文件固定 UTF-8，2MB 轮转留 3 份。
- `_safe_stream()` 解决了一个真实的 Windows 问题：控制台是 GBK 代码页时，
  往 `stderr` 写中文日志会抛 `UnicodeEncodeError` 把程序带崩。它会尝试
  `reconfigure(encoding="utf-8", errors="replace")`，流不支持就原样返回。

```python
# 典型用法 —— 由 build_application() 调用，路径跟着「当前数据目录」走
setup(log_file=data / "logs" / "diangeji.log")
log = get_logger(__name__)
log.info("HTTP 服务已启动：%s", url)
```

> **`log_file` 一定要显式传。** `setup()` 不给路径时不会写文件；
> 而 `build_application()` 早先就是无参调用，日志于是永远落在默认
> `data_dir()` 里 —— `--data-dir D:\foo` 只搬走了配置和曲库，
> 用户去新目录找日志会扑空（CHANGELOG 49）。
