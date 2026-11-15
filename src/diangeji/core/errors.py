"""领域异常。

统一根类 ``DianGeJiError``，上层可以一把 ``except DianGeJiError`` 兜住
所有**预期内**的业务失败；未捕获的才是真 bug。

每个类带一个 ``http_status``，让 HTTP 层不必逐个 ``isinstance`` 判断：
* 400 = 请求方给的东西不对，用户可以自己纠正（关键词空了、曲目不存在）
* 500 = 服务端自己坏了（模板缺失、安装不完整），用户改不了
"""

from __future__ import annotations


class DianGeJiError(Exception):
    """本项目所有业务异常的根类。"""

    #: 默认按「用户输入有问题」处理，子类按需覆盖
    http_status = 400


class EmptyKeywordError(DianGeJiError, ValueError):
    """点歌关键词为空。

    同时继承 ``ValueError`` 是刻意的：调用方（含老代码、第三方脚本）可能
    只 ``except ValueError``，改抛领域异常时不该把它们一起弄崩。
    """
    http_status = 400


class TrackNotFoundError(DianGeJiError):
    """本地曲库和在线音源都没找到这首歌。"""
    http_status = 400


class NoPlayableTrackError(DianGeJiError):
    """找到了曲目但拿不到可播放地址（VIP / 版权 / 直链过期）。"""
    http_status = 400


class LibraryUnavailableError(DianGeJiError):
    """曲库目录不可读 / 模板缺失 —— 服务端这边的问题，不是用户能改的。"""
    http_status = 500


class DuplicateTrackError(DianGeJiError, ValueError):
    """同一首歌已经在队列里（可选启用）。"""
    http_status = 400
