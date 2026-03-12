# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates/index.html', 'templates'),
        ('icon.ico', '.')  # Include the icon file
    ],
    hiddenimports=[
        'flask',
        'flask_cors',
        'serial',
        'serial.tools.list_ports',
        'webview',
        'logging',
        'datetime',
        'json',
        'os',
        'pathlib',
        'threading',
        'time',
        'csv',
        'werkzeug.serving',
        'atexit',
        'signal',
        'sys'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='BLDC Motor Testing System',  # Name of your executable
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Set to False to hide console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',  # Path to your icon file
)