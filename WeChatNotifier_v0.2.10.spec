# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('data/notify.wav', 'data')]
binaries = []
hiddenimports = ['pythoncom', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont',
                 # v0.2.9: gate 判定层是函数内延迟 import，显式声明避免被裁掉；rip_xrefs 已去 numpy 依赖
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
        # v0.2.9: rip_xrefs 已改为纯 Python 实现，numpy 彻底不需要；
        # 这里的排除只用于防止打包环境残留 numpy/scipy 拖入 19MB openblas
        'numpy', 'scipy', 'numpy.core', 'numpy.libs',
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
    name='WeChatNotifier_v0.2.10',
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
    name='WeChatNotifier_v0.2.10',
)
