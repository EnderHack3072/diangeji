"""点歌姬 —— B 站直播间弹幕点歌机。

分层结构（详见 ``docs/architecture.md``）::

    diangeji/
        paths.py, constants.py, config.py, logging_setup.py   引导层：路径 / 常量 / 配置 / 日志
        core/        领域模型、本地曲库、队列、错误
        sources/     音源：本地曲库 / QQ 音乐（可扩展的 MusicSource 协议）
        live/        B 站弹幕：WebSocket 客户端、弹幕客户端、指令解析
        auth/        扫码登录与 Cookie 持久化
        services/    应用服务：播放编排、解析调度、曲库扫描
        web/         本地 HTTP 服务（主播台 / OBS 覆盖层 / API / 媒体）
        ui/          Tkinter 主播端
        utils/       无业务含义的小工具

依赖方向严格单向：``ui`` / ``web`` → ``services`` → ``core`` / ``sources`` / ``live`` → ``utils``。
底层模块不得反向导入上层模块。
"""

# 版本号只在 ``constants.py`` 里定义一份；``pyproject.toml`` 是打包必须的
# 第二份（构建工具读不到 Python 代码），两处要一起改。
# 这里做成 re-export 而不是重复字面量 —— 原来 ``__init__`` 和 ``constants``
# 各写一遍 "1.1.0"，改漏一处就是发布事故。
from .constants import APP_NAME as __app_name__, APP_VERSION as __version__

__all__ = ["__version__", "__app_name__"]
