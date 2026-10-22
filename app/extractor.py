import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional

from .layer_config import LayerInfo, classify_file, is_renderable

SEVEN_ZIP_URL = "https://www.7-zip.org"


def find_7zip() -> Optional[str]:
    """Locate 7z.exe: PATH, the usual install folders, then the registry."""
    found = shutil.which("7z") or shutil.which("7z.exe")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramW6432"), r"C:\Program Files", r"C:\Program Files (x86)"):
        if base:
            candidate = os.path.join(base, "7-Zip", "7z.exe")
            if os.path.isfile(candidate):
                return candidate
    if sys.platform.startswith("win"):
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\7-Zip") as key:
                        path = winreg.QueryValueEx(key, "Path")[0]
                    candidate = os.path.join(path, "7z.exe")
                    if os.path.isfile(candidate):
                        return candidate
                except OSError:
                    continue
        except ImportError:
            pass
    return None


def _extract_zip(archive_path: str, extract_dir: str) -> None:
    """Extract a ZIP with Python, refusing entries that would land outside extract_dir."""
    root = os.path.realpath(extract_dir)
    with zipfile.ZipFile(archive_path) as z:
        for member in z.infolist():
            target = os.path.realpath(os.path.join(root, member.filename))
            if os.path.commonpath([root, target]) != root:
                raise RuntimeError(f"Unsafe path in archive: {member.filename}")
        z.extractall(root)


def _extract_7z(archive_path: str, extract_dir: str) -> None:
    seven_zip = find_7zip()
    if not seven_zip:
        raise RuntimeError(
            "7-Zip is needed to open RAR and 7z archives, but it was not found.\n\n"
            f"Install it from {SEVEN_ZIP_URL}, or repack the Gerber files as a ZIP "
            "(ZIP files open without 7-Zip)."
        )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash in the windowed EXE
    result = subprocess.run(
        [seven_zip, "x", archive_path, f"-o{extract_dir}", "-y"],
        capture_output=True, text=True, creationflags=flags,
    )
    if result.returncode != 0:
        raise RuntimeError(f"7-Zip extraction failed:\n{result.stderr or result.stdout}")


def extract_archive(archive_path: str) -> str:
    """Extract a ZIP / RAR / 7z archive into a new temporary folder and return it.

    Every archive gets its own empty folder, so layers from previously opened
    boards can never mix in. Call cleanup_extraction() when done with it.
    """
    archive_path = str(Path(archive_path).resolve())
    extract_dir = tempfile.mkdtemp(prefix="gerber_viewer_")
    try:
        if zipfile.is_zipfile(archive_path):
            _extract_zip(archive_path, extract_dir)
        else:
            _extract_7z(archive_path, extract_dir)
    except Exception:
        cleanup_extraction(extract_dir)
        raise
    return extract_dir


def cleanup_extraction(extract_dir: Optional[str]) -> None:
    """Delete a folder created by extract_archive (only ever our own temp folders)."""
    if not extract_dir:
        return
    tmp = os.path.realpath(tempfile.gettempdir())
    path = os.path.realpath(extract_dir)
    if os.path.basename(path).startswith("gerber_viewer_") and os.path.dirname(path) == tmp:
        shutil.rmtree(path, ignore_errors=True)


def discover_layers(root_dir: str) -> List[LayerInfo]:
    """Walk extracted directory and return all renderable layer files."""
    layers: List[LayerInfo] = []

    for dirpath, _, filenames in os.walk(root_dir):
        for fname in sorted(filenames):
            full_path = os.path.join(dirpath, fname)
            if not is_renderable(full_path):
                continue
            layer_type = classify_file(full_path)
            ext = Path(full_path).suffix.upper()
            layers.append(LayerInfo(
                path=full_path,
                filename=fname,
                layer_type=layer_type,
                extension=ext,
            ))

    # Sort by layer order
    from .layer_config import LAYER_ORDER
    layers.sort(key=lambda l: LAYER_ORDER.get(l.layer_type, 99))
    return layers
