import os
import subprocess
from pathlib import Path
from typing import List

from .layer_config import LayerInfo, classify_file, is_renderable, EXTENSION_MAP

SEVEN_ZIP = r"C:\Program Files\7-Zip\7z.exe"


def extract_archive(archive_path: str) -> str:
    """Extract a RAR/ZIP archive using 7-Zip. Returns the extraction directory."""
    archive_path = str(Path(archive_path).resolve())
    extract_dir = str(Path(archive_path).parent / ".pcb_extracted")

    os.makedirs(extract_dir, exist_ok=True)

    result = subprocess.run(
        [SEVEN_ZIP, "x", archive_path, f"-o{extract_dir}", "-y"],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"7-Zip extraction failed:\n{result.stderr}")

    return extract_dir


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
