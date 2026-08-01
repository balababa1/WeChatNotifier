# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['wechat_notifier.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['pythoncom', 'win32com', 'win32com.client', 'uiautomation', 'comtypes'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL._avif', 'PIL._webp', 'PIL._imagingcms', 'win32ui', 'lzma'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WeChatNotifier_v0.2.4',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['data\\wechat_new.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WeChatNotifier_v0.2.4',
)
