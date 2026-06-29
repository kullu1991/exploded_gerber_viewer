# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

block_cipher = None

pyvista_datas    = collect_data_files('pyvista')
pyvistaqt_datas  = collect_data_files('pyvistaqt')
pygerber_datas   = collect_data_files('pygerber')
vtkmodules_datas = collect_data_files('vtkmodules')
pyqt6_datas      = collect_data_files('PyQt6')
qtpy_datas       = collect_data_files('qtpy')

all_datas = (
    pyvista_datas +
    pyvistaqt_datas +
    pygerber_datas +
    vtkmodules_datas +
    pyqt6_datas +
    qtpy_datas
)

vtk_bins = collect_dynamic_libs('vtkmodules')

hidden = (
    collect_submodules('vtkmodules') +
    collect_submodules('pyvista') +
    collect_submodules('pyvistaqt') +
    collect_submodules('pygerber') +
    collect_submodules('qtpy') +
    collect_submodules('packaging') +
    [
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'PyQt6.QtOpenGL',
        'PyQt6.QtOpenGLWidgets',
        'PyQt6.sip',
        'PIL.Image',
        'PIL._imaging',
        'numpy',
        'scipy',
        'scipy.spatial',
        'scipy.spatial.transform._rotation_groups',
        'packaging',
        'packaging.version',
        'packaging.specifiers',
        'packaging.requirements',
    ]
)

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=vtk_bins,
    datas=all_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['rthook_qt_api.py'],   # sets QT_API=pyqt6 before any import
    excludes=['tkinter', 'IPython', 'jupyter'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GerberViewer',
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
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='GerberViewer',
)
