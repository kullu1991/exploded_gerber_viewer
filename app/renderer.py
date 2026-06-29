import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple, List, Dict

from PIL import Image

from .layer_config import LayerInfo, LayerType, LAYER_SOLID_COLOR, LAYER_ORDER

log = logging.getLogger(__name__)

# Silence noisy pygerber deprecation warnings
logging.getLogger("root").setLevel(logging.ERROR)


@dataclass
class RenderedLayer:
    info: LayerInfo
    image: Image.Image
    min_x_mm: float
    max_x_mm: float
    min_y_mm: float
    max_y_mm: float


def _make_color_scheme(layer_type: LayerType):
    from pygerber.gerberx3.api import ColorScheme, RGBA
    r, g, b, a = LAYER_SOLID_COLOR[layer_type]
    return ColorScheme(
        background_color=RGBA(r=0, g=0, b=0, a=0),
        clear_color=RGBA(r=0, g=0, b=0, a=0),
        solid_color=RGBA(r=r, g=g, b=b, a=a),
        clear_region_color=RGBA(r=0, g=0, b=0, a=0),
        solid_region_color=RGBA(r=r, g=g, b=b, a=a),
        debug_1_color=RGBA(r=180, g=0, b=0, a=128),
        debug_2_color=RGBA(r=0, g=180, b=0, a=128),
    )


def render_layer(info: LayerInfo, dpi: int = 300) -> Optional[RenderedLayer]:
    """Render a single Gerber file to a PIL RGBA image. Returns None on failure."""
    if info.layer_type == LayerType.DRILL:
        return None  # Excellon not yet supported in rasterizer path

    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # Suppress pygerber root logger temporarily
            root_log = logging.getLogger()
            old_level = root_log.level
            root_log.setLevel(logging.CRITICAL)

            from pygerber.gerberx3.api import Rasterized2DLayer, Rasterized2DLayerParams

            scheme = _make_color_scheme(info.layer_type)
            params = Rasterized2DLayerParams(
                source_path=info.path,
                colors=scheme,
                dpi=dpi,
            )
            layer = Rasterized2DLayer(params)
            result = layer.render()
            root_log.setLevel(old_level)

        image = result.get_image()
        props = result.get_properties()
        bb = props.gerber_bounding_box

        return RenderedLayer(
            info=info,
            image=image,
            min_x_mm=float(bb.min_x.value),
            max_x_mm=float(bb.max_x.value),
            min_y_mm=float(bb.min_y.value),
            max_y_mm=float(bb.max_y.value),
        )

    except Exception as e:
        log.warning("Failed to render %s: %s", info.filename, e)
        return None


def recolor_image(
    image: Image.Image,
    r: int, g: int, b: int,
) -> Image.Image:
    """Return a copy of *image* with all non-transparent pixels set to (r, g, b)."""
    import numpy as np
    arr = np.array(image.convert("RGBA"), dtype=np.uint8).copy()
    mask = arr[:, :, 3] > 0
    arr[mask, 0] = r
    arr[mask, 1] = g
    arr[mask, 2] = b
    return Image.fromarray(arr, "RGBA")


def composite_layers(
    rendered: List[RenderedLayer],
    visible: Dict[str, bool],
    dpi: int = 300,
    background: Tuple[int, int, int] = (30, 30, 30),
    color_overrides: Optional[Dict[str, Tuple[int, int, int]]] = None,
) -> Image.Image:
    """Composite all visible rendered layers into one RGBA image."""
    active = [
        r for r in rendered
        if visible.get(r.info.filename, True)
    ]

    if not active:
        return Image.new("RGBA", (800, 600), (*background, 255))

    gmin_x = min(r.min_x_mm for r in active)
    gmax_x = max(r.max_x_mm for r in active)
    gmin_y = min(r.min_y_mm for r in active)
    gmax_y = max(r.max_y_mm for r in active)

    px_per_mm = dpi / 25.4
    canvas_w = max(1, int((gmax_x - gmin_x) * px_per_mm) + 2)
    canvas_h = max(1, int((gmax_y - gmin_y) * px_per_mm) + 2)

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (*background, 255))

    active_sorted = sorted(
        active,
        key=lambda r: LAYER_ORDER.get(r.info.layer_type, 99),
    )

    for rl in active_sorted:
        off_x = int((rl.min_x_mm - gmin_x) * px_per_mm)
        off_y = int((gmax_y - rl.max_y_mm) * px_per_mm)
        img = rl.image
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        # Apply per-layer color override if set
        if color_overrides and rl.info.filename in color_overrides:
            img = recolor_image(img, *color_overrides[rl.info.filename])
        canvas.paste(img, (off_x, off_y), img)

    return canvas


def composite_side(
    rendered: List[RenderedLayer],
    side_types: set,
    dpi: int = 300,
    background: Tuple[int, int, int] = (30, 30, 30),
) -> Image.Image:
    """Composite only layers belonging to the given side types."""
    visible = {r.info.filename: (r.info.layer_type in side_types) for r in rendered}
    return composite_layers(rendered, visible, dpi, background)


def get_board_size_mm(rendered: List[RenderedLayer]) -> Tuple[float, float]:
    """Return (width_mm, height_mm) of the full board."""
    if not rendered:
        return (100.0, 80.0)
    gmin_x = min(r.min_x_mm for r in rendered)
    gmax_x = max(r.max_x_mm for r in rendered)
    gmin_y = min(r.min_y_mm for r in rendered)
    gmax_y = max(r.max_y_mm for r in rendered)
    return (gmax_x - gmin_x, gmax_y - gmin_y)


def get_global_bbox(rendered: List[RenderedLayer]) -> Tuple[float, float, float, float]:
    """Return (min_x, max_x, min_y, max_y) in mm across all layers."""
    if not rendered:
        return (0, 100, 0, 80)
    return (
        min(r.min_x_mm for r in rendered),
        max(r.max_x_mm for r in rendered),
        min(r.min_y_mm for r in rendered),
        max(r.max_y_mm for r in rendered),
    )


def render_layer_to_canvas(
    rl: RenderedLayer,
    gmin_x: float, gmax_x: float,
    gmin_y: float, gmax_y: float,
    dpi: int,
) -> Image.Image:
    """Place a single rendered layer into a global-sized transparent RGBA canvas.

    rl.image may have been rendered at a different DPI than `dpi` (e.g. the 2-D
    render uses 300 DPI while the 3-D worker caps at 150).  We must resize the
    image to the target DPI before pasting, otherwise a 2× larger image pasted
    at 1× offsets clips half the content and makes layers look zoomed/cropped.
    """
    px_per_mm = dpi / 25.4
    canvas_w = max(1, int((gmax_x - gmin_x) * px_per_mm) + 2)
    canvas_h = max(1, int((gmax_y - gmin_y) * px_per_mm) + 2)

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    img = rl.image if rl.image.mode == "RGBA" else rl.image.convert("RGBA")

    # Resize to the size this layer *should* be at the target DPI
    target_w = max(1, int((rl.max_x_mm - rl.min_x_mm) * px_per_mm))
    target_h = max(1, int((rl.max_y_mm - rl.min_y_mm) * px_per_mm))
    if img.width != target_w or img.height != target_h:
        img = img.resize((target_w, target_h), Image.LANCZOS)

    off_x = int((rl.min_x_mm - gmin_x) * px_per_mm)
    off_y = int((gmax_y - rl.max_y_mm) * px_per_mm)
    canvas.paste(img, (off_x, off_y), img)
    return canvas
