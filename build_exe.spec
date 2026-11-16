# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""PyInstaller 打包配置：把 GUI + Python 解释器打进同一个 exe。

产物：bilimusic.exe
  · 双击          → 启动 GUI
  · --run-py 脚本  → 当解释器用，执行脚本（无需外部 Python）

路径来源：优先用环境变量 BILI_SRC（构建脚本传入），否则按当前工作目录推断。
（PyInstaller 执行 spec 时不注入 __file__，而 SPECPATH 在某些情况下会被截断，
所以这里显式用环境变量传递，最稳。）
"""
import os

from PyInstaller.utils.hooks import collect_submodules

SRC = os.environ.get('BILI_SRC') or os.path.join(os.getcwd(), 'bilimusic')
if not os.path.isdir(SRC):
    raise SystemExit(f'找不到源码目录: {SRC}（请设置环境变量 BILI_SRC）')

hidden = []
try:
    hidden += collect_submodules('requests')
except Exception:
    pass
hidden += ['tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox',
           'tkinter.scrolledtext', 'encodings.idna', 'ssl', 'certifi']

a = Analysis(
    [os.path.join(SRC, 'gui', 'launcher.py')],
    pathex=[SRC],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'numpy', 'pandas', 'matplotlib', 'PIL', 'scipy'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='bilimusic',
    icon=os.path.join(SRC, 'gui', 'bili.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无控制台窗口；--run-py 用 fd 1/2 重绑 stdout
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
