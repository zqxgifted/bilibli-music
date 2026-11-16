# SPDX-License-Identifier: MIT
"""依赖探测与解析。

查找顺序（对每个组件都一样）：
    1. config/user.json 里手动指定的路径（用户在设置页填的，最高优先）
    2. 包内 bin\\ 目录        —— 完整包自带
    3. 系统 PATH              —— 用户已装并加进环境变量
    4. 常见安装位置            —— ffmpeg 常见路径等

两个组件：
    ffmpeg   —— 音频封装/转码（必需）
    ffprobe  —— 校验音频时长（必需）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEPENDENCY_INFO = {
    "ffmpeg": {
        "label": "ffmpeg",
        "why": "把下载的音轨封装成 m4a / 转成 mp3",
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases",
        "home": "https://ffmpeg.org/download.html",
        "hint": ("下载 ffmpeg-master-latest-win64-gpl.zip，解压后把里面的 ffmpeg.exe "
                 "放进 bin 文件夹；或把它的 bin 目录加到系统 PATH"),
        "file": "ffmpeg.exe",
    },
    "ffprobe": {
        "label": "ffprobe",
        "why": "校验已下载音频的时长是否与清单一致",
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases",
        "home": "https://ffmpeg.org/download.html",
        "hint": "和 ffmpeg 在同一个包里，一起放进 bin 文件夹即可",
        "file": "ffprobe.exe",
    },
}


@dataclass
class Resolved:
    """单个组件的解析结果。"""
    name: str
    path: str | None = None
    source: str = ""          # "bundle" | "path" | "common" | "config" | ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.path)

    @property
    def from_bundle(self) -> bool:
        return self.source == "bundle"


def _root() -> Path:
    """包根目录：打包后 = exe 所在目录；源码运行 = 项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _root()


def _common_locations() -> dict:
    """各组件在 Windows 上的常见安装位置。"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "")
    return {
        "ffmpeg": [
            rf"{pf}\ffmpeg\bin\ffmpeg.exe",
            r"D:\ffmpeg\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
            rf"{local}\Microsoft\WinGet\Links\ffmpeg.exe",
            rf"{local}\Programs\ffmpeg\bin\ffmpeg.exe",
            r"D:\Library\tools\ffmpeg\bin\ffmpeg.exe",
        ],
        "ffprobe": [
            rf"{pf}\ffmpeg\bin\ffprobe.exe",
            r"D:\ffmpeg\bin\ffprobe.exe",
            r"C:\ffmpeg\bin\ffprobe.exe",
            rf"{local}\Microsoft\WinGet\Links\ffprobe.exe",
            rf"{local}\Programs\ffmpeg\bin\ffprobe.exe",
            r"D:\Library\tools\ffmpeg\bin\ffprobe.exe",
        ],
    }


def _config_value(key: str):
    """读 config/user.json 里的配置（不抛异常）。"""
    p = ROOT / "config" / "user.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")).get(key)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def resolve(name: str) -> Resolved:
    """解析一个组件的位置。"""
    info = DEPENDENCY_INFO[name]
    exe_name = info["file"]

    # 1) 用户手动指定（config/user.json 里的 ffmpeg / ffprobe）
    cfg = _config_value(name)
    if cfg and str(cfg).strip():
        p = Path(str(cfg).strip())
        if p.is_file():
            return Resolved(name, str(p), "config")

    # 2) 包内 bin\
    bundle = ROOT / "bin" / exe_name
    if bundle.is_file():
        return Resolved(name, str(bundle), "bundle")

    # 3) 系统 PATH
    found = shutil.which(exe_name) or shutil.which(name)
    if found:
        return Resolved(name, found, "path")

    # 4) 常见位置
    for cand in _common_locations().get(name, []):
        if cand and Path(cand).is_file():
            return Resolved(name, cand, "common")

    return Resolved(name, None, "")


def resolve_all() -> dict:
    return {n: resolve(n) for n in DEPENDENCY_INFO}


def missing(resolved: dict | None = None) -> list:
    r = resolved or resolve_all()
    return [n for n, v in r.items() if not v.ok]


def build_env(base: dict | None = None) -> dict:
    """构造子进程环境：优先用解析到的真实路径。"""
    env = dict(base or os.environ)
    bindir = ROOT / "bin"
    if bindir.is_dir():
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    for name in ("ffmpeg", "ffprobe"):
        r = resolve(name)
        if r.ok and not r.from_bundle:
            d = str(Path(r.path).parent)
            if d not in env.get("PATH", ""):
                env["PATH"] = d + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def summary_lines(resolved: dict | None = None) -> list:
    """返回 [(状态, 名称, 说明)]，给 GUI / 日志展示。"""
    r = resolved or resolve_all()
    src_label = {"bundle": "包内自带", "path": "系统 PATH",
                 "common": "常见安装位置", "config": "设置里指定", "": ""}
    out = []
    for name, res in r.items():
        info = DEPENDENCY_INFO[name]
        if res.ok:
            out.append(("ok", info["label"],
                        f"{src_label.get(res.source, res.source)}：{res.path}"))
        else:
            out.append(("miss", info["label"], info["why"]))
    return out


if __name__ == "__main__":
    r = resolve_all()
    for status, label, text in summary_lines(r):
        mark = "OK  " if status == "ok" else "MISS"
        print(f"[{mark}] {label:10s} {text}")
