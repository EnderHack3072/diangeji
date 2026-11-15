# 模块详解

> 按子包逐个说明：**它负责什么、不负责什么、对外接口、内部实现要点、
> 出错时怎么表现、改它要注意什么**。
>
> 架构总览（分层规则、数据流、线程模型）见 [../architecture.md](../architecture.md)。
> 想快速上手改代码，先看 [../architecture.md](../architecture.md) 的「新功能放哪」。

## 索引

| 子包 | 一句话 | 文档 |
|---|---|---|
| `paths.py` `config.py` `constants.py` `logging_setup.py` | 地基：路径、配置、常量、日志 | [infra.md](infra.md) |
| `core/` | 纯逻辑：数据模型、播放队列、本地曲库 | [core.md](core.md) |
| `sources/` | 音源：本地 / QQ 音乐，统一接口 | [sources.md](sources.md) |
| `services/` | 应用服务：点歌解析、曲库扫描、空闲随机 | [services.md](services.md) |
| `live/` | B 站弹幕：协议、WBI 签名、指令解析、长连接 | [live.md](live.md) |
| `auth/` | 登录：cookie 存储、扫码登录、浏览器 CDP 抓 cookie | [auth.md](auth.md) |
| `web/` | 本地 HTTP：路由、安全校验、模板渲染、服务生命周期 | [web.md](web.md) |
| `ui/` | Tkinter 界面（逻辑沿用 v1.0，只做了拆分） | [ui.md](ui.md) |
| `utils/` | 无业务依赖的小工具 | [utils.md](utils.md) |

## 依赖方向（一句话版）

```
ui / web  →  services  →  core / sources / live  →  utils
                              ↓
                        paths / config / constants / logging_setup
```

**只能往下，不能往上，也不能左右横跳。** `tests/test_layering.py` 用 AST
扫真实 import，违反就红。

`app.py` 是唯一的装配点，`__main__.py` 是唯一的入口。
`ui` 不 import `app`（否则表现层依赖装配层，反向依赖），
实际装配在 `__main__.run_gui()` 里做，装好了把 `app` 当参数传给 `ui.launch()`。

## 各模块共同遵守的三条

1. **写文件一律 `paths.atomic_write()`** —— 断电不留半个文件，临时文件名
   带 pid + 线程 id + 随机后缀（固定 `.tmp` 在多线程下会互相踩）。
2. **要比对再改状态，用 CAS 形式** —— 例如
   `queue.advance_if_playing(id)` 把「比对 id」和「占播放位」放进同一把锁。
   分两步写（先读再改）在并发下会出双跳歌那种 bug。
3. **锁外不做网络** —— 需要联网的地方先放锁、拿数据、再重新加锁确认状态
   仍然有效（`advance()` / `previous()` 都是这个模式）。
