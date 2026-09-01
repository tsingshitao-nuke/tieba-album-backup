# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec —— 阶段二「CDP 瘦身版」onedir 打包。
# 产物: dist/贴吧相册备份工具(瘦身版)/贴吧相册备份工具(瘦身版).exe
#
# 与 Playwright 版的区别：
#   - 浏览器后端改为 CDP（系统 Edge/Chrome + websocket-client），**不打包 playwright**
#     → 体积从 ~137MB 降到 ~15MB。
#   - 代价：要求目标机器已装 Microsoft Edge 或 Google Chrome（Windows 10/11 自带 Edge）。
#   - 代码路径完全一致，只有 BrowserBackend 的实现不同。

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules("tbalbum")
hiddenimports += ["crawler", "app"]
# CDP 后端依赖 websocket-client（包名 websocket）；lxml 用于相册列表解析
hiddenimports += collect_submodules("websocket")
hiddenimports += collect_submodules("lxml")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 瘦身版不需要 playwright（及其 node 驱动）
        "playwright",
        # 常见但用不到的大型库，排除以减小体积
        "numpy", "pandas", "matplotlib", "PIL", "PyQt5", "PySide2",
        "IPython", "notebook", "pytest", "tkinter.test",
    ],
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
    name="贴吧相册备份工具(瘦身版)",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
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
    name="贴吧相册备份工具(瘦身版)",
)
