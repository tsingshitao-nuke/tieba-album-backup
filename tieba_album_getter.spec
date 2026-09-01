# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec —— 阶段一「Playwright 版」onedir 打包。
# 产物: dist/贴吧相册备份工具/贴吧相册备份工具.exe
#
# 说明:
#   - 显式收集 playwright 的数据/动态库/driver（node.exe + package）。
#     浏览器复用系统 Microsoft Edge（channel="msedge"），不打包 Chromium。
#   - crawler.py 里 playwright 是**函数内延迟导入**（只有相册列表页需要它），
#     静态分析扫不到，因此必须靠 hiddenimports 把 playwright 与 tbalbum 全量子模块收进来。
#   - 阶段二另有 slim 版（cdp 后端，去掉 playwright）见 tieba_album_getter_slim.spec。

import os
from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                     collect_submodules)

datas, binaries, hiddenimports = [], [], []

# ---- playwright（仅相册列表页需要，但必须打进来）----
datas += collect_data_files("playwright")
binaries += collect_dynamic_libs("playwright")

import playwright as _plw_mod
_driver_dir = os.path.join(os.path.dirname(_plw_mod.__file__), "driver")
if os.path.isdir(_driver_dir):
    datas.append((_driver_dir, "playwright/driver"))

# ---- 子模块：playwright 与本项目 tbalbum 包，全量收集最稳 ----
hiddenimports += collect_submodules("playwright")
hiddenimports += ["playwright.sync_api", "playwright.async_api"]
hiddenimports += collect_submodules("tbalbum")
hiddenimports += ["crawler", "app"]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="贴吧相册下载器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 纯图形界面；异常会显示在界面日志区
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="贴吧相册下载器",
)
