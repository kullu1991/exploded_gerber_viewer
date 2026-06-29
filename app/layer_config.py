from enum import Enum
from dataclasses import dataclass
from typing import Dict, Tuple


class LayerType(Enum):
    TOP_COPPER    = "Top Copper"
    BOT_COPPER    = "Bottom Copper"
    INNER_COPPER  = "Inner Layer"
    TOP_SILK      = "Top Silkscreen"
    BOT_SILK      = "Bottom Silkscreen"
    TOP_MASK      = "Top Solder Mask"
    BOT_MASK      = "Bottom Solder Mask"
    TOP_PASTE     = "Top Paste"
    BOT_PASTE     = "Bottom Paste"
    BOARD_OUTLINE = "Board Outline"
    MECHANICAL    = "Mechanical"
    DRILL         = "Drill"
    UNKNOWN       = "Unknown"


@dataclass
class LayerInfo:
    path: str
    filename: str
    layer_type: LayerType
    extension: str
    visible: bool = True
    rendered: bool = False
    failed: bool = False


# RGBA solid color for each layer type (r, g, b, a)
LAYER_SOLID_COLOR: Dict[LayerType, Tuple[int, int, int, int]] = {
    LayerType.TOP_COPPER:    (255, 180,   0, 255),
    LayerType.BOT_COPPER:    (  0, 120, 220, 255),
    LayerType.INNER_COPPER:  (220, 100,   0, 200),
    LayerType.TOP_SILK:      (255, 255, 255, 255),
    LayerType.BOT_SILK:      (200, 210, 255, 255),
    LayerType.TOP_MASK:      (  0, 200,  60, 130),
    LayerType.BOT_MASK:      (  0,  60, 200, 130),
    LayerType.TOP_PASTE:     (180, 180, 180, 200),
    LayerType.BOT_PASTE:     (150, 150, 150, 200),
    LayerType.BOARD_OUTLINE: (255, 220,   0, 255),
    LayerType.MECHANICAL:    (100, 100, 200, 200),
    LayerType.DRILL:         ( 40,  40,  40, 255),
    LayerType.UNKNOWN:       (200, 200, 200, 200),
}

# Hex color for sidebar swatch
LAYER_DISPLAY_COLOR: Dict[LayerType, str] = {
    LayerType.TOP_COPPER:    "#FFB400",
    LayerType.BOT_COPPER:    "#0078DC",
    LayerType.INNER_COPPER:  "#DC6400",
    LayerType.TOP_SILK:      "#FFFFFF",
    LayerType.BOT_SILK:      "#C8D2FF",
    LayerType.TOP_MASK:      "#00C83C",
    LayerType.BOT_MASK:      "#003CC8",
    LayerType.TOP_PASTE:     "#B4B4B4",
    LayerType.BOT_PASTE:     "#969696",
    LayerType.BOARD_OUTLINE: "#FFDC00",
    LayerType.MECHANICAL:    "#6464C8",
    LayerType.DRILL:         "#282828",
    LayerType.UNKNOWN:       "#C8C8C8",
}

# Render order (lower = drawn first / bottom of stack)
LAYER_ORDER: Dict[LayerType, int] = {
    LayerType.BOARD_OUTLINE: 0,
    LayerType.BOT_MASK:      1,
    LayerType.BOT_COPPER:    2,
    LayerType.BOT_PASTE:     3,
    LayerType.BOT_SILK:      4,
    LayerType.INNER_COPPER:  5,
    LayerType.MECHANICAL:    6,
    LayerType.TOP_PASTE:     7,
    LayerType.TOP_MASK:      8,
    LayerType.TOP_COPPER:    9,
    LayerType.TOP_SILK:      10,
    LayerType.DRILL:         11,
    LayerType.UNKNOWN:       12,
}

# File extension → LayerType (uppercase)
EXTENSION_MAP: Dict[str, LayerType] = {
    ".GTL":  LayerType.TOP_COPPER,
    ".GBL":  LayerType.BOT_COPPER,
    ".G1":   LayerType.INNER_COPPER,
    ".G2":   LayerType.INNER_COPPER,
    ".G3":   LayerType.INNER_COPPER,
    ".G4":   LayerType.INNER_COPPER,
    # .GD* and .GG* are Altium documentation/keepout layers with unpredictable
    # bounding boxes — including them distorts the global canvas and misaligns layers.
    ".GG1":  LayerType.INNER_COPPER,
    ".GG2":  LayerType.INNER_COPPER,
    ".GG3":  LayerType.INNER_COPPER,
    ".GG4":  LayerType.INNER_COPPER,
    ".GG5":  LayerType.INNER_COPPER,
    ".GPT":  LayerType.TOP_COPPER,
    ".GPB":  LayerType.BOT_COPPER,
    ".GTO":  LayerType.TOP_SILK,
    ".GBO":  LayerType.BOT_SILK,
    ".GTS":  LayerType.TOP_MASK,
    ".GBS":  LayerType.BOT_MASK,
    ".GTP":  LayerType.TOP_PASTE,
    ".GBP":  LayerType.BOT_PASTE,
    ".GKO":  LayerType.BOARD_OUTLINE,
    ".GM":   LayerType.MECHANICAL,
    ".GM1":  LayerType.MECHANICAL,
    ".GM2":  LayerType.MECHANICAL,
    ".GM15": LayerType.MECHANICAL,
    ".DRL":  LayerType.DRILL,
    ".TX1":  LayerType.DRILL,
    ".TX2":  LayerType.DRILL,
    ".TX3":  LayerType.DRILL,
    ".TX4":  LayerType.DRILL,
    ".DR1":  LayerType.DRILL,
    ".DR2":  LayerType.DRILL,
    ".DR3":  LayerType.DRILL,
    ".DR4":  LayerType.DRILL,
    ".XLN":  LayerType.DRILL,
    ".NC":   LayerType.DRILL,
}

# Extensions that are definitely not renderable layers
SKIP_EXTENSIONS = {
    ".APR_LIB", ".APR", ".REP", ".DRR", ".EXTREP", ".LDP",
    ".PDF", ".CSV", ".XML", ".ZIP",
    # Altium documentation layers — bounding boxes extend far outside PCB outline
    ".GD1", ".GD2", ".GD3", ".GD4", ".GD5",
}

# Top-side layer types (for 3D texture compositing)
TOP_LAYER_TYPES = {
    LayerType.TOP_COPPER, LayerType.TOP_SILK,
    LayerType.TOP_MASK, LayerType.TOP_PASTE,
    LayerType.BOARD_OUTLINE,
}

# Bottom-side layer types
BOTTOM_LAYER_TYPES = {
    LayerType.BOT_COPPER, LayerType.BOT_SILK,
    LayerType.BOT_MASK, LayerType.BOT_PASTE,
}


def classify_file(path: str) -> LayerType:
    """Classify a file by its extension."""
    import os
    ext = os.path.splitext(path)[1].upper()
    return EXTENSION_MAP.get(ext, LayerType.UNKNOWN)


def is_renderable(path: str) -> bool:
    """Return True if the file is likely a Gerber/Excellon layer."""
    import os
    ext = os.path.splitext(path)[1].upper()
    if ext in SKIP_EXTENSIONS:
        return False
    # Skip plain text reports
    if ext == ".TXT":
        try:
            with open(path, "r", errors="ignore") as f:
                first = f.read(10).strip()
            # Excellon drill files start with % or M48
            return first.startswith("%") or first.upper().startswith("M48")
        except Exception:
            return False
    return ext in EXTENSION_MAP
