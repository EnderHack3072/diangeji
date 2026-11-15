"""领域异常。

统一根类 ``DianGeJiError``，上层可以一把 ``except DianGeJiError`` 兜住
所有**预期内**的业务失败；未捕获的才是真 bug。
"""

from __future__ import annotations


class DianGeJiError(Exception):
    """本项目所有业务异常的根类。"""


class EmptyKeywordError(DianGeJiError):
    """点歌关键词为空。"""


class TrackNotFoundError(DianGeJiError):
    """本地曲库和在线音源都没找到这首歌。"""


class NoPlayableTrackError(DianGeJiError):
    """找到了曲目但拿不到可播放地址（VIP / 版权 / 直链过期）。"""


class LibraryUnavailableError(DianGeJiError):
    """曲库目录不可读。"""


class DuplicateTrackError(DianGeJiError):
    """同一首歌已经在队列里（可选启用）。"""
