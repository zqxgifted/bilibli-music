# SPDX-License-Identifier: MIT
"""bilimusic core —— B站歌下载工具箱的核心逻辑。

模块划分：
    config      分层配置加载
    naming      文件名 / 歌名清洗
    rules       判定规则引擎（标题 → 是不是目标在唱）
    bili_api    B站接口访问（view / playurl / 空间列表 / 搜索）
    fetcher     抓投稿列表（全量 / 增量）
    dedup       两层去重 + 基底 + 文档生成
    downloader  下载音频 + 校验 + 过期文件归位
    pipeline    编排：把上面几步串成用户看得懂的「步骤」
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["config", "naming", "rules", "bili_api", "fetcher", "dedup",
           "downloader", "pipeline"]
