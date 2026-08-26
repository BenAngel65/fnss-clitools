"""fnsswatch — 文件夹监控双向同步守护进程。

监控本地目录的文件变化，实时推送到 fnss 服务端；
同时定期轮询远端变更，拉取到本地。
"""
from __future__ import annotations

__version__ = "0.2.9"
