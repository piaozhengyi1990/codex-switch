# -*- mode: python ; coding: utf-8 -*-
# Codex 模型切换器 — PyInstaller 单文件窗口程序打包配置
#
# 结构参考 D:\Programs\pdf转word\PdfToWord.spec（已验证可用），
# 差异：
#   1. 运行期零第三方依赖，因此没有 hiddenimports / collect_data_files
#   2. upx=False —— UPX 加壳是杀毒软件与 SmartScreen 误报的常见诱因，
#      本工具要分发给同事，稳定性优先于体积。

a = Analysis(
    ['codex_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')],
    hiddenimports=['codex_cn'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='CodexSwitch',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 窗口程序，不带黑色控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
    version='version.txt',
    manifest='app.manifest',   # 高 DPI 感知 + Common Controls v6 主题
)
