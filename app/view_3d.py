import os
import logging
from collections import defaultdict
from typing import Dict, Optional, Tuple

import numpy as np

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QSlider, QPushButton,
    QStackedWidget, QToolBar, QProgressBar,
)
from PyQt6.QtCore import Qt

from .layer_config import (
    LayerInfo, LayerType, LAYER_DISPLAY_COLOR,
    TOP_LAYER_TYPES, BOTTOM_LAYER_TYPES,
)

os.environ.setdefault("QT_API", "pyqt6")

try:
    import pyvista as pv
    from pyvistaqt import QtInteractor
    _HAS_PYVISTA = True
except Exception:
    _HAS_PYVISTA = False

# ── Z-slot for exploded view (one slot per layer type) ──────────────
_TYPE_Z: Dict[LayerType, float] = {
    LayerType.BOT_SILK:      0,
    LayerType.BOT_MASK:      1,
    LayerType.BOT_PASTE:     2,
    LayerType.BOT_COPPER:    3,
    LayerType.BOARD_OUTLINE: 4,
    LayerType.INNER_COPPER:  5,
    LayerType.MECHANICAL:    6,
    LayerType.TOP_COPPER:    7,
    LayerType.TOP_PASTE:     8,
    LayerType.TOP_MASK:      9,
    LayerType.TOP_SILK:     10,
    LayerType.DRILL:         4,
    LayerType.UNKNOWN:      11,
}

_PCB_GREEN = np.array([28, 115, 48], dtype=np.float32)   # solder-mask green


def _layer_z(info: LayerInfo, inner_index: int = 0) -> float:
    base = _TYPE_Z.get(info.layer_type, 11.0)
    if info.layer_type == LayerType.INNER_COPPER:
        base += inner_index * 0.8
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

    def clear(self):
        self._layer_images.clear()
        self._actors.clear()
        self._visible.clear()
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

    # ── dispatch ───────────────────────────────

    def _render(self):
        if not self._layer_images or not self._plotter:
            return
        self._plotter.clear()
        self._actors.clear()

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

    def _type_groups(self):
        """Group visible layers by type → {LayerType: sorted list of (arr,info,z)}."""
        groups = defaultdict(list)
        for arr, info, z in self._visible_items():
            groups[info.layer_type].append((arr, info, z))
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

    # ── exploded mode ──────────────────────────

    def _render_exploded(self):
        w, h   = self._board_size_mm
        H, W   = self._canvas_size()
        spread = self._spread
        groups = self._type_groups()

        if not groups:
            return

        for layer_type, items in sorted(groups.items(), key=lambda kv: _TYPE_Z.get(kv[0], 11)):
            z_slot = _TYPE_Z.get(layer_type, 11)
            z_pos  = z_slot * spread

            # RGBA composite — transparent where no Gerber data.
            # Depth peeling (enabled above) makes these transparent areas see-through.
            comp_rgba = _alpha_composite_rgba(items, H, W)
            plane     = _board_plane(w, h, z_pos)
            actor     = self._plotter.add_mesh(
                plane, texture=_to_texture(comp_rgba), show_edges=False, opacity=1.0,
            )
            self._actors[layer_type.value] = actor

        # Light wireframe showing the full stack envelope
        all_z = [_TYPE_Z.get(lt, 11) * spread for lt in groups]
        box = pv.Box(bounds=(0, w, 0, h,
                             min(all_z) - spread * 0.2,
                             max(all_z) + spread * 0.2))
        self._plotter.add_mesh(
            box, color="#1b5e20", opacity=0.04,
            show_edges=True, edge_color="#388e3c", line_width=1,
        )

    def _update_exploded_visibility(self):
        """Fast visibility update without full re-render (exploded mode only)."""
        groups = self._type_groups()
        for layer_type, actor in list(self._actors.items()):
            try:
                # Find the LayerType whose value matches the key
                lt = next(lt for lt in LayerType if lt.value == layer_type)
            except StopIteration:
                continue
            actor.SetVisibility(lt in groups)
