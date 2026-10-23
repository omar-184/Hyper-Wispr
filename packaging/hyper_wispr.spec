# PyInstaller spec. Build with:  pyinstaller packaging/hyper_wispr.spec
# Set HW_GPU=1 to bundle the NVIDIA CUDA libraries (needs the [gpu] extra installed).
import glob
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = collect_data_files("faster_whisper") + collect_data_files("onnxruntime")
binaries = collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime") + collect_dynamic_libs("sounddevice")
datas += collect_data_files("_sounddevice_data")
hiddenimports = (
    collect_submodules("hyper_wispr")
    + collect_submodules("pynput")
    + collect_submodules("pystray")
    + ["ctranslate2", "onnxruntime", "av"]
)

if os.environ.get("HW_GPU") == "1":
    import nvidia

    for root in nvidia.__path__:
        for sub in ("cublas", "cudnn"):
            for dll in glob.glob(os.path.join(root, sub, "bin", "*.dll")):
                # Next to ctranslate2.dll, which adds its own folder to the DLL search path.
                binaries.append((dll, "ctranslate2"))

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Hyper-Wispr",
    console=False,
    icon=os.path.join(SPECPATH, "icon.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="Hyper-Wispr")
