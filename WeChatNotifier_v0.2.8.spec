# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('data/notify.wav', 'data')]
binaries = []
hiddenimports = ['pythoncom', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont',
                 # v0.2.8: gate 判定层是函数内延迟 import，显式声明避免被裁掉
                 'wechat_uia']
tmp_ret = collect_all('uiautomation')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['wechat_notifier.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 弹窗抗锯齿需要 Pillow，不再整体排除 PIL；
    # 仅裁剪与渲染无关的模块/插件以控制体积（保留 _imaging / _imagingft 等核心）
    excludes=[
        'PIL.ImageQt', 'PIL.ImageTk', 'PIL.ImageShow', 'PIL.ImageGrab',
        'PIL._avif', 'PIL._webp', 'PIL.ImageSequence', 'PIL.PdfParser',
        'PIL.ImageMath', 'PIL.MpoImagePlugin', 'PIL.PyAccess',
        # 体积控制：wechat_uia 里 numpy 只用于加速 200MB DLL 扫描，
        # 缺失时自动退回纯 re，功能不受影响；缓存命中后根本不扫描
        'numpy',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WeChatNotifier_v0.2.8',
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
    name='WeChatNotifier_v0.2.8',
)
