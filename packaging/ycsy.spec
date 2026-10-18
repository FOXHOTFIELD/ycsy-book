# PyInstaller spec: builds a single-file executable with no Python required.
#
#   pip install pyinstaller
#   pyinstaller packaging/ycsy.spec --noconfirm
#
# Output: dist/ycsy.exe (Windows) or dist/ycsy (macOS/Linux)

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# SPECPATH is the spec file itself; the project root is one level above packaging/
SPEC = Path(SPECPATH).resolve()
ROOT = SPEC.parent.parent if SPEC.suffix == ".spec" else SPEC.parent

datas, binaries, hiddenimports = collect_all("rich")
datas += [(str(ROOT / "README.md"), "."), (str(ROOT / "LICENSE"), ".")]

analysis = Analysis(
    [str(ROOT / "packaging" / "ycsy_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide2", "pytest", "numpy", "scipy",
              "pandas", "matplotlib", "IPython", "notebook", "sqlalchemy",
              "PIL", "mkl"],
)

pyz = PYZ(analysis.pure, analysis.zipped_data)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="ycsy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # the TUI needs a real console window
    disable_windowed_traceback=False,
    icon=None,
)
