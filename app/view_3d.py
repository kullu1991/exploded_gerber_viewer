import os
import re
import logging
from collections import defaultdict
from typing import Dict, Optional, Tuple

import numpy as np

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QPushButton,
    QStackedWidget, QToolBar, QProgressBar,
)
from PyQt6.QtCore import Qt

from .layer_config import (
    LayerInfo, LayerType, LAYER_DISPLAY_COLOR,
    TOP_LAYER_TYPES, BOTTOM_LAYER_TYPES, LAYER_ORDER,
)

os.environ.setdefault("QT_API", "pyqt6")

try:
    import pyvista as pv
    from pyvistaqt import QtInteractor
    _HAS_PYVISTA = True
except Exception:
    _HAS_PYVISTA = False

# ── Z-slot base values for the exploded view ────────────────────────
# INNER_COPPER layers each get their own slot: base + index*step
# INNER_PLANE  layers are all composited into one slot at their base
_IC_BASE  = 4.5   # z_slot base for INNER_COPPER (G1, G2 …)
_IC_STEP  = 0.8   # z_slot step between successive inner copper layers
_IP_BASE  = 5.8   # z_slot for all INNER_PLANE layers (GG*, composited together)

_TYPE_Z: Dict[LayerType, float] = {
    LayerType.BOT_SILK:      0,
    LayerType.BOT_MASK:      1,
    LayerType.BOT_PASTE:     2,
    LayerType.BOT_COPPER:    3,
    LayerType.BOARD_OUTLINE: 4,
    LayerType.INNER_COPPER:  _IC_BASE,  # per-layer, separated by _IC_STEP
    LayerType.INNER_PLANE:   _IP_BASE,  # all merged into one plane
    LayerType.MECHANICAL:    6,
    LayerType.TOP_COPPER:    7,
    LayerType.TOP_PASTE:     8,
    LayerType.TOP_MASK:      9,
    LayerType.TOP_SILK:     10,
    LayerType.DRILL:         4,
    LayerType.UNKNOWN:      11,
}

_PCB_GREEN = np.array([28, 115, 48], dtype=np.float32)   # solder-mask green


def _name_to_z_slot(layer_name: str) -> float:
    """Convert an Altium layer name (from DRR) to a Z-slot value.

    Matches: 'Top Layer', 'Bottom Layer', 'Int1 (GND)', 'Int2 (PWR)', etc.
    Uses the same constants as _layer_z so via endpoints coincide with planes.
    """
    n = layer_name.lower()
    if "top" in n:
        return float(_TYPE_Z[LayerType.TOP_COPPER])
    if "bottom" in n:
        return float(_TYPE_Z[LayerType.BOT_COPPER])
    # Inner layer: "Int1 (GND)" → index 0, "Int2 (PWR)" → index 1 …
    m = re.search(r"int\s*(\d+)", n)
    idx = (int(m.group(1)) - 1) if m else 0
    return _IC_BASE + idx * _IC_STEP


def _layer_z(info: LayerInfo, inner_index: int = 0) -> float:
    """Z-slot for a layer.  Inner copper layers are separated by _IC_STEP."""
    base = _TYPE_Z.get(info.layer_type, 11.0)
    if info.layer_type == LayerType.INNER_COPPER:
        base += inner_index * _IC_STEP
    # INNER_PLANE: all share the same _IP_BASE slot (composited together)
    return base


def _alpha_composite(layers, bg: np.ndarray, H: int, W: int) -> np.ndarray:
    """Alpha-composite a list of (RGBA arr, ...) tuples over bg → RGB uint8."""
    canvas = np.full((H, W, 3), bg, dtype=np.float32)
    for arr, *_ in layers:
        a = arr[:, :, 3:4].astype(np.float32) / 255.0
        canvas = canvas * (1.0 - a) + arr[:, :, :3].astype(np.float32) * a
    return np.clip(canvas, 0, 255).astype(np.uint8)


def _alpha_composite_rgba(layers, H: int, W: int) -> np.ndarray:
    """Alpha-composite a list of (RGBA arr, ...) tuples → RGBA uint8.

    Transparent where no layer has data, so depth-peeling transparency works.
    """
    rgb   = np.zeros((H, W, 3), dtype=np.float32)
    alpha = np.zeros((H, W, 1), dtype=np.float32)
    for arr, *_ in layers:
        a   = arr[:, :, 3:4].astype(np.float32) / 255.0
        rgb = rgb * (1.0 - a) + arr[:, :, :3].astype(np.float32) * a
        alpha = np.clip(alpha + a * 255.0, 0.0, 255.0)
    out = np.concatenate([np.clip(rgb, 0, 255).astype(np.uint8),
                          alpha.astype(np.uint8)], axis=2)
    return out


def _to_texture(arr: np.ndarray) -> "pv.Texture":
    """Create a pyvista texture from an RGB or RGBA C-contiguous uint8 array."""
    return pv.numpy_to_texture(np.ascontiguousarray(arr))


def _board_plane(w: float, h: float, z: float, direction=(0, 0, 1)):
    return pv.Plane(
        center=(w / 2, h / 2, z),
        direction=direction,
        i_size=w, j_size=h,
        i_resolution=1, j_resolution=1,
    )


class Viewer3D(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._plotter: Optional["QtInteractor"] = None
        # {filename: (RGBA ndarray, LayerInfo, z_slot)}
        self._layer_images: Dict[str, tuple] = {}
        self._actors: Dict[str, object] = {}
        self._board_size_mm: Tuple[float, float] = (100.0, 80.0)
        self._spread = 5.0
        self._mode = "board"
        self._visible: Dict[str, bool] = {}
        # Via / drill data
        self._via_holes: list = []
        self._via_origin: Tuple[float, float] = (0.0, 0.0)
        self._show_vias: bool = True
        self._via_actors: list = []
        # Cumulative rotation applied via sliders (degrees, delta-based)
        self._rot_x: int = 0   # elevation
        self._rot_y: int = 0   # azimuth
        self._rot_z: int = 0   # roll

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if not _HAS_PYVISTA:
            lbl = QLabel(
                "3D view requires pyvista and pyvistaqt.\n"
                "Run:  .venv\\Scripts\\pip install pyvista pyvistaqt"
            )
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl)
            return

        layout.addWidget(self._build_toolbar())
        layout.addWidget(self._build_rotation_bar())

        self._stack = QStackedWidget()

        # Page 0 — placeholder + progress bar
        ph_widget = QWidget()
        ph_layout = QVBoxLayout(ph_widget)
        ph_layout.setContentsMargins(40, 0, 40, 0)
        self._placeholder = QLabel("Switch to this tab after loading a PCB.")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet("color:#555; font-size:14px;")
        self._3d_progress = QProgressBar()
        self._3d_progress.setRange(0, 100)
        self._3d_progress.setTextVisible(True)
        self._3d_progress.setFormat("Building 3D view… %v / %m layers")
        self._3d_progress.setFixedHeight(20)
        self._3d_progress.setStyleSheet(
            "QProgressBar{background:#1a1a2a;border:none;color:#aaa;font-size:11px;}"
            "QProgressBar::chunk{background:#2e7d32;}"
        )
        self._3d_progress.hide()
        ph_layout.addStretch()
        ph_layout.addWidget(self._placeholder)
        ph_layout.addSpacing(16)
        ph_layout.addWidget(self._3d_progress)
        ph_layout.addStretch()
        self._stack.addWidget(ph_widget)

        self._plotter = QtInteractor(self)
        self._plotter.set_background("#0a0a14")
        self._stack.addWidget(self._plotter)
        layout.addWidget(self._stack)

        tip = QLabel("  Rotate: left-drag   Zoom: scroll   Pan: right-drag")
        tip.setStyleSheet("color:#333; font-size:10px; padding:2px;")
        layout.addWidget(tip)

    # ── toolbar ────────────────────────────────

    def _build_toolbar(self) -> QToolBar:
        tb = QToolBar()
        tb.setMovable(False)

        self._btn_board    = self._mode_btn(tb, "Board",    "board")
        self._btn_exploded = self._mode_btn(tb, "Exploded", "exploded")
        self._btn_board.setChecked(True)
        tb.addSeparator()

        tb.addWidget(QLabel("  Spread: "))
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(1, 60)
        self._slider.setValue(int(self._spread))
        self._slider.setFixedWidth(160)
        self._slider.valueChanged.connect(self._on_spread)
        self._spread_lbl = QLabel(f"{self._spread:.0f} mm")
        self._spread_lbl.setStyleSheet("color:#aaa; min-width:38px;")
        tb.addWidget(self._slider)
        tb.addWidget(self._spread_lbl)
        tb.addSeparator()

        for label, mode in [("Top", "top"), ("Iso", "iso"), ("Side", "side")]:
            b = QPushButton(label)
            b.setFixedHeight(24)
            b.setFixedWidth(38)
            b.clicked.connect(lambda _, m=mode: self._set_camera(m))
            tb.addWidget(b)

        tb.addSeparator()
        self._btn_vias = QPushButton("Vias")
        self._btn_vias.setCheckable(True)
        self._btn_vias.setChecked(True)
        self._btn_vias.setFixedHeight(24)
        self._btn_vias.setFixedWidth(40)
        self._btn_vias.setToolTip("Show/hide drill holes and vias")
        self._btn_vias.clicked.connect(self._on_via_toggle)
        tb.addWidget(self._btn_vias)

        self._sync_toolbar()
        return tb

    def _mode_btn(self, tb, label, mode):
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setFixedHeight(24)
        btn.clicked.connect(lambda _, m=mode: self._set_mode(m))
        tb.addWidget(btn)
        return btn

    def _sync_toolbar(self):
        spread = self._mode == "exploded"
        self._slider.setVisible(spread)
        self._spread_lbl.setVisible(spread)

    def _build_rotation_bar(self) -> QWidget:
        """Compact bar with X / Y / Z rotation sliders (delta-based)."""
        bar = QWidget()
        bar.setStyleSheet("background:#1a1a2a;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 2, 8, 2)
        row.setSpacing(6)

        axes = [
            ("X", "_rot_x", "_slider_x", "_lbl_x", "Elevation — tilt up / down"),
            ("Y", "_rot_y", "_slider_y", "_lbl_y", "Azimuth  — rotate left / right"),
            ("Z", "_rot_z", "_slider_z", "_lbl_z", "Roll"),
        ]
        for axis, attr, sl_attr, lbl_attr, tip in axes:
            lbl = QLabel(f"{axis}:")
            lbl.setStyleSheet("color:#888; font-size:11px;")
            lbl.setFixedWidth(16)
            row.addWidget(lbl)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(-180, 180)
            slider.setValue(0)
            slider.setToolTip(tip)
            slider.setFixedWidth(130)
            slider.setStyleSheet(
                "QSlider::groove:horizontal{height:4px;background:#333;border-radius:2px;}"
                "QSlider::handle:horizontal{width:12px;height:12px;margin:-4px 0;"
                "background:#5a8fd4;border-radius:6px;}"
            )
            setattr(self, sl_attr, slider)
            row.addWidget(slider)

            val_lbl = QLabel("  0°")
            val_lbl.setStyleSheet("color:#aaa; font-size:11px; min-width:34px;")
            setattr(self, lbl_attr, val_lbl)
            row.addWidget(val_lbl)

            # Capture loop vars in closure
            def _handler(v, _attr=attr, _sl=slider, _lbl=val_lbl):
                old = getattr(self, _attr)
                delta = v - old
                setattr(self, _attr, v)
                _lbl.setText(f"{v:4d}°")
                if self._plotter and delta != 0:
                    if _attr == "_rot_x":
                        self._plotter.camera.Elevation(delta)
                    elif _attr == "_rot_y":
                        self._plotter.camera.Azimuth(delta)
                    else:
                        self._plotter.camera.Roll(delta)
                    self._plotter.render()

            slider.valueChanged.connect(_handler)

            if axis != "Z":
                sep = QLabel("  ")
                row.addWidget(sep)

        btn_reset = QPushButton("Reset")
        btn_reset.setFixedHeight(20)
        btn_reset.setFixedWidth(46)
        btn_reset.setStyleSheet("font-size:10px; padding:0;")
        btn_reset.setToolTip("Reset all rotations to 0")
        btn_reset.clicked.connect(self._reset_rotation)
        row.addWidget(btn_reset)

        row.addStretch()
        return bar

    def _reset_rotation(self):
        """Reset rotation sliders and camera to isometric view."""
        if self._plotter:
            self._plotter.view_isometric()
            self._plotter.reset_camera()
            self._plotter.render()
        self._reset_rotation_state()

    def _on_via_toggle(self, checked: bool):
        self._show_vias = checked
        if self._via_actors and self._plotter:
            for actor in self._via_actors:
                actor.SetVisibility(checked)
            self._plotter.render()
        elif self._layer_images:
            self._render()

    # ── public API ─────────────────────────────

    def update_layer_images(
        self,
        layer_images: Dict[str, tuple],
        board_size_mm: Tuple[float, float],
        initial_visible: Optional[Dict[str, bool]] = None,
    ):
        self._layer_images = layer_images
        self._board_size_mm = board_size_mm
        self._visible = {k: True for k in layer_images} if initial_visible is None \
                        else dict(initial_visible)
        if _HAS_PYVISTA and self._plotter:
            self._stack.setCurrentIndex(1)
            self._render()

    def toggle_layer(self, filename: str, visible: bool):
        self._visible[filename] = visible
        if not self._layer_images or not self._plotter:
            return
        if self._mode == "exploded":
            self._update_exploded_visibility()
            self._plotter.render()
        else:
            self._render()

    def set_all_visible(self, visible: bool):
        self._visible = {k: visible for k in self._visible}
        if not self._layer_images or not self._plotter:
            return
        if self._mode == "exploded":
            self._update_exploded_visibility()
            self._plotter.render()
        else:
            self._render()

    def show_progress(self, done: int, total: int):
        if not _HAS_PYVISTA:
            return
        self._stack.setCurrentIndex(0)
        self._placeholder.setText("Building 3D view…")
        self._3d_progress.setRange(0, total)
        self._3d_progress.setValue(done)
        self._3d_progress.show()

    def hide_progress(self):
        if not _HAS_PYVISTA:
            return
        self._3d_progress.hide()
        if self._layer_images:
            self._stack.setCurrentIndex(1)

    def set_loading(self, loading: bool):
        if not _HAS_PYVISTA:
            return
        if loading:
            self._placeholder.setText("Building 3D view…")
            self._stack.setCurrentIndex(0)
        elif self._layer_images:
            self._stack.setCurrentIndex(1)

    def update_layer_color(self, filename: str, r: int, g: int, b: int):
        """Recolor a layer's stored RGBA array and re-render the scene."""
        if filename not in self._layer_images:
            return
        arr, info, z_slot = self._layer_images[filename]
        arr = arr.copy()
        mask = arr[:, :, 3] > 0
        arr[mask, 0] = r
        arr[mask, 1] = g
        arr[mask, 2] = b
        self._layer_images[filename] = (arr, info, z_slot)
        if self._layer_images and self._plotter:
            self._render()

    def set_vias(self, holes: list, gmin_x: float, gmin_y: float):
        """Store parsed drill holes and re-render if 3D is already built."""
        self._via_holes = holes
        self._via_origin = (gmin_x, gmin_y)
        if self._layer_images and self._plotter:
            self._render()

    def clear(self):
        self._layer_images.clear()
        self._actors.clear()
        self._visible.clear()
        self._via_holes.clear()
        if _HAS_PYVISTA and self._plotter:
            self._plotter.clear()
            self._plotter.render()
        if hasattr(self, "_stack"):
            self._placeholder.setText("Switch to this tab after loading a PCB.")
            self._stack.setCurrentIndex(0)

    # ── internals ──────────────────────────────

    def _set_mode(self, mode: str):
        self._mode = mode
        self._btn_board.setChecked(mode == "board")
        self._btn_exploded.setChecked(mode == "exploded")
        self._sync_toolbar()
        if self._layer_images:
            self._render()

    def _on_spread(self, v: int):
        self._spread = float(v)
        self._spread_lbl.setText(f"{v} mm")
        if self._layer_images and self._mode == "exploded":
            self._render()

    def _set_camera(self, mode: str):
        if not self._plotter:
            return
        {"top": self._plotter.view_xy,
         "iso": self._plotter.view_isometric,
         "side": self._plotter.view_xz}[mode]()
        self._plotter.render()
        if hasattr(self, "_slider_x"):
            self._reset_rotation_state()

    def _reset_rotation_state(self):
        """Silently zero the slider values and internal rotation counters."""
        for sl, lbl, attr in [
            (self._slider_x, self._lbl_x, "_rot_x"),
            (self._slider_y, self._lbl_y, "_rot_y"),
            (self._slider_z, self._lbl_z, "_rot_z"),
        ]:
            sl.blockSignals(True)
            sl.setValue(0)
            sl.blockSignals(False)
            lbl.setText("  0°")
            setattr(self, attr, 0)

    # ── dispatch ───────────────────────────────

    def _render(self):
        if not self._layer_images or not self._plotter:
            return
        self._plotter.clear()
        self._actors.clear()
        self._via_actors.clear()

        # Depth peeling lets RGBA textures render correctly in Exploded mode.
        # Use pyvista's high-level API which is safer than direct VTK calls.
        if self._mode == "exploded":
            try:
                self._plotter.enable_depth_peeling(number_of_peels=4,
                                                   occlusion_ratio=0.0)
            except Exception:
                pass  # GPU/driver doesn't support it; layers will be opaque
        else:
            try:
                self._plotter.disable_depth_peeling()
            except Exception:
                pass

        try:
            if self._mode == "board":
                self._render_board()
            else:
                self._render_exploded()
        except Exception as e:
            logging.warning("3D render error: %s", e, exc_info=True)
        self._plotter.view_isometric()
        self._plotter.reset_camera()
        self._plotter.render()
        if hasattr(self, "_slider_x"):
            self._reset_rotation_state()

    # ── compositing helpers ────────────────────

    def _canvas_size(self):
        first = next(iter(self._layer_images.values()))[0]
        return first.shape[0], first.shape[1]   # H, W

    def _visible_items(self, type_filter=None):
        """Return list of (arr, info, z) for visible layers, optionally filtered by type."""
        out = []
        for fname, (arr, info, z) in self._layer_images.items():
            if not self._visible.get(fname, True):
                continue
            if type_filter is not None and info.layer_type not in type_filter:
                continue
            out.append((arr, info, z))
        out.sort(key=lambda x: x[2])
        return out

    def _type_groups(self) -> dict:
        """Group visible layers for the exploded view.

        Key: (LayerType, z_slot_key)
          - INNER_COPPER: one group per distinct z_slot → separate plane per G1/G2/…
          - INNER_PLANE : all merged under one fixed z_slot → one composite plane
          - Everything else: one group per type.
        """
        groups: dict = defaultdict(list)
        for arr, info, z in self._visible_items():
            if info.layer_type == LayerType.INNER_COPPER:
                key = (info.layer_type, round(z, 2))   # z already = layer's z_slot
            elif info.layer_type == LayerType.INNER_PLANE:
                key = (info.layer_type, _IP_BASE)       # all merged
            else:
                key = (info.layer_type, _TYPE_Z.get(info.layer_type, 11))
            groups[key].append((arr, info, z))
        for v in groups.values():
            v.sort(key=lambda x: x[2])
        return groups

    # ── board mode ─────────────────────────────

    def _render_board(self):
        w, h = self._board_size_mm
        H, W = self._canvas_size()

        top_types = TOP_LAYER_TYPES | {LayerType.BOARD_OUTLINE}
        bot_types  = BOTTOM_LAYER_TYPES | {LayerType.BOARD_OUTLINE}

        top_rgb = _alpha_composite(self._visible_items(top_types),  _PCB_GREEN, H, W)
        bot_rgb = _alpha_composite(self._visible_items(bot_types),   _PCB_GREEN, H, W)

        thickness = 1.6

        # FR4 substrate
        board = pv.Box(bounds=(0, w, 0, h, 0, thickness))
        self._plotter.add_mesh(board, color="#1b5e20", show_edges=False)

        # Top face — slightly proud of the substrate
        self._plotter.add_mesh(
            _board_plane(w, h, thickness + 0.02),
            texture=_to_texture(top_rgb), show_edges=False,
        )

        # Bottom face — direction=(0,0,-1) mirrors U, matching physical flip
        self._plotter.add_mesh(
            _board_plane(w, h, -0.02, direction=(0, 0, -1)),
            texture=_to_texture(bot_rgb), show_edges=False,
        )

        # Vias through the board
        self._render_vias(z_bottom=-0.02, z_top=thickness + 0.02)

    # ── exploded mode ──────────────────────────

    def _render_exploded(self):
        w, h   = self._board_size_mm
        H, W   = self._canvas_size()
        spread = self._spread
        groups = self._type_groups()   # {(LayerType, z_slot_key): items}

        if not groups:
            return

        for (layer_type, z_slot_key), items in sorted(
            groups.items(), key=lambda kv: kv[0][1]   # sort by z_slot_key
        ):
            z_pos     = z_slot_key * spread
            comp_rgba = _alpha_composite_rgba(items, H, W)
            plane     = _board_plane(w, h, z_pos)
            actor     = self._plotter.add_mesh(
                plane, texture=_to_texture(comp_rgba), show_edges=False, opacity=1.0,
            )
            # Actor key: include z_slot for types that have per-layer planes
            actor_key = f"{layer_type.value}|{z_slot_key:.2f}"
            self._actors[actor_key] = actor

        # Light wireframe
        all_z = [zk * spread for (_, zk) in groups]
        z_bottom, z_top = min(all_z), max(all_z)
        try:
            box = pv.Box(bounds=(0, w, 0, h,
                                 z_bottom - spread * 0.2,
                                 z_top    + spread * 0.2))
            self._plotter.add_mesh(box, color="#1b5e20", opacity=0.04,
                                   show_edges=True, edge_color="#388e3c", line_width=1)
        except Exception:
            pass

        self._render_vias(z_bottom=z_bottom, z_top=z_top)

    def _render_vias(self, z_bottom: float, z_top: float):
        """Draw drill holes as cylinders, each spanning only its own layer pair.

        In Board mode  z_bottom/z_top are the substrate faces (fixed 1.6 mm).
        In Exploded mode they are the stack extents; each hole's cylinder
        reaches from its DRR 'from' layer plane to its 'to' layer plane,
        with a small margin so it visibly pokes through each plane.
        """
        if not self._via_holes:
            return

        gmin_x, gmin_y = self._via_origin
        w, h   = self._board_size_mm
        spread = self._spread

        # In Board mode all vias span the same fixed range.
        board_mode = (self._mode == "board")

        # Group holes by layer-pair so we can build one merged mesh per group.
        # Key: (layer_from, layer_to, plated)
        groups: dict = defaultdict(list)
        for hole in self._via_holes:
            xw = hole.x_mm - gmin_x
            yw = hole.y_mm - gmin_y
            if xw < -2 or xw > w + 2 or yw < -2 or yw > h + 2:
                continue
            groups[(hole.layer_from, hole.layer_to, hole.plated)].append((xw, yw, hole))

        for (lyr_from, lyr_to, plated), items in groups.items():
            if board_mode:
                z0, z1 = z_bottom, z_top
            else:
                # Cylinders stop exactly at each layer plane — no protrusion
                zf = _name_to_z_slot(lyr_from) * spread
                zt = _name_to_z_slot(lyr_to)   * spread
                z0 = min(zf, zt)
                z1 = max(zf, zt)

            height   = max(0.1, z1 - z0)
            z_center = (z0 + z1) / 2
            color    = "#c8960c" if plated else "#909090"

            meshes = []
            for xw, yw, hole in items:
                radius = max(0.15, hole.diameter_mm / 2)
                try:
                    meshes.append(pv.Cylinder(
                        center=(xw, yw, z_center),
                        direction=(0, 0, 1),
                        radius=radius,
                        height=height,
                        resolution=12,
                    ))
                except Exception:
                    continue

            if not meshes:
                continue
            merged = meshes[0]
            for m in meshes[1:]:
                merged = merged.merge(m)
            actor = self._plotter.add_mesh(merged, color=color, show_edges=False)
            actor.SetVisibility(self._show_vias)
            self._via_actors.append(actor)

    def _update_exploded_visibility(self):
        """Fast visibility update without full re-render (exploded mode only)."""
        groups = self._type_groups()
        active_keys = {f"{lt.value}|{zk:.2f}" for lt, zk in groups}
        for actor_key, actor in list(self._actors.items()):
            actor.SetVisibility(actor_key in active_keys)
