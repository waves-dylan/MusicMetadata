# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/app_icon.ico', 'assets'), ('assets/app_icon.png', 'assets')],
    hiddenimports=['PIL.Image'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'numpy', 'scipy', 'pandas', 'matplotlib',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'Crypto', 'Cryptodome', 'cryptography',
        'requests', 'urllib3', 'charset_normalizer', 'certifi', 'idna',
        'unittest', 'pydoc', 'doctest', 'xmlrpc', 'curses', 'lib2to3', 'IPython',
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SongMetadata',
    icon='assets/app_icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
