import os
from typing import Dict, List, Optional, Tuple

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter,
    QTabWidget, QLabel, QCheckBox, QPushButton, QFileDialog,
    QGroupBox, QSpinBox, QStatusBar, QScrollArea, QMessageBox,
    QProgressBar,
)
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtCore import Qt, QThread, QObject, pyqtSignal

import numpy as np
from PIL import Image

from .layer_config import (
    LayerInfo, LayerType, LAYER_DISPLAY_COLOR, LAYER_ORDER,
)
from .extractor import extract_archive, discover_layers
from .renderer import (
    RenderedLayer, render_layer, composite_layers,
    get_board_size_mm, get_global_bbox, render_layer_to_canvas,
)
from .view_2d import Viewer2D
from .view_3d import Viewer3D, _layer_z  # _layer_z used by ThreeDWorker
from .drill_parser import parse_drill_files, parse_drr, DrillHole


# ──────────────────────────────────────────────
# Worker: render Gerber layers (background)
# ──────────────────────────────────────────────

class RenderWorker(QObject):
    layer_done = pyqtSignal(object)   # RenderedLayer | None
    finished   = pyqtSignal()
    progress   = pyqtSignal(int, int) # done, total

    def __init__(self, layers: List[LayerInfo], dpi: int):
        super().__init__()
        self._layers = layers
        self._dpi = dpi
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        total = len(self._layers)
        for i, info in enumerate(self._layers):
            if self._cancelled:
                break
            result = render_layer(info, self._dpi)
            if result is not None:
                result.info.rendered = True
            else:
                info.failed = True
            self.layer_done.emit(result)
            self.progress.emit(i + 1, total)
        self.finished.emit()


# ──────────────────────────────────────────────
# Worker: build per-layer canvases for 3D (background)
# ──────────────────────────────────────────────

class ThreeDWorker(QObject):
    """Composites each visible layer into a global-bbox canvas, returns numpy arrays."""
    ready    = pyqtSignal(object)   # dict {filename: (np.ndarray, LayerInfo, z_slot)}
    progress = pyqtSignal(int, int)

    def __init__(self, rendered: List[RenderedLayer], dpi: int):
        super().__init__()
        self._rendered  = rendered
        self._dpi       = min(dpi, 150)  # cap texture DPI for performance
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        if not self._rendered:
            self.ready.emit({})
            return

        gmin_x, gmax_x, gmin_y, gmax_y = get_global_bbox(self._rendered)
        total = len(self._rendered)

        # Separate counters: INNER_COPPER (G1,G2…) and INNER_PLANE (GG*) each
        # get their own sequence so z_slots stay within the correct Z range.
        inner_copper_counter = 0
        inner_plane_counter  = 0
        result: Dict[str, tuple] = {}

        for i, rl in enumerate(self._rendered):
            if self._cancelled:
                break
            canvas = render_layer_to_canvas(
                rl, gmin_x, gmax_x, gmin_y, gmax_y, self._dpi
            )
            if canvas.mode != "RGBA":
                canvas = canvas.convert("RGBA")
            arr = np.array(canvas)  # shape (H, W, 4)

            key = rl.info.filename
            if rl.info.layer_type == LayerType.INNER_COPPER:
                idx = inner_copper_counter
                inner_copper_counter += 1
            elif rl.info.layer_type == LayerType.INNER_PLANE:
                idx = inner_plane_counter
                inner_plane_counter += 1
            else:
                idx = 0
            z_slot = _layer_z(rl.info, idx)
            result[key] = (arr, rl.info, z_slot)
            self.progress.emit(i + 1, total)

        self.ready.emit(result)


# ──────────────────────────────────────────────
# Layer panel row widget
# ──────────────────────────────────────────────

class LayerRow(QWidget):
    toggled       = pyqtSignal(str, bool)
    color_changed = pyqtSignal(str, int, int, int)   # filename, r, g, b

    def __init__(self, info: LayerInfo, parent=None):
        super().__init__(parent)
        self._info = info
        self._color_hex = LAYER_DISPLAY_COLOR.get(info.layer_type, "#888888")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        # Clickable color swatch — opens QColorDialog
        self._swatch = QPushButton()
        self._swatch.setFixedSize(16, 16)
        self._swatch.setToolTip("Click to change layer colour")
        self._swatch.clicked.connect(self._pick_color)
        self._apply_swatch()

        self._check = QCheckBox(info.layer_type.value)
        self._check.setChecked(True)
        self._check.setStyleSheet("color:#ddd; font-size:12px;")

        ext_lbl = QLabel(info.extension)
        ext_lbl.setStyleSheet("color:#666; font-size:10px;")

        layout.addWidget(self._swatch)
        layout.addWidget(self._check)
        layout.addStretch()
        layout.addWidget(ext_lbl)

        self._check.toggled.connect(lambda v: self.toggled.emit(info.filename, v))

    def _apply_swatch(self):
        self._swatch.setStyleSheet(
            f"background:{self._color_hex}; border:1px solid #555;"
            f" border-radius:2px; padding:0;"
        )

    def _pick_color(self):
        from PyQt6.QtWidgets import QColorDialog
        from PyQt6.QtGui import QColor
        initial = QColor(self._color_hex)
        color = QColorDialog.getColor(
            initial, self,
            f"Layer colour — {self._info.layer_type.value} ({self._info.filename})",
        )
        if color.isValid():
            self._color_hex = color.name()
            self._apply_swatch()
            self.color_changed.emit(
                self._info.filename, color.red(), color.green(), color.blue()
            )

    def filename(self) -> str:
        return self._info.filename


# ──────────────────────────────────────────────
# Main window
# ──────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Gerber PCB Viewer")
        self.setAcceptDrops(True)

        self._layers:     List[LayerInfo]     = []
        self._rendered:   List[RenderedLayer]  = []
        self._visible:    Dict[str, bool]      = {}
        self._layer_colors: Dict[str, Tuple[int,int,int]] = {}  # filename → (r,g,b)
        self._dpi: int = 300
        self._layer_rows: List[LayerRow]       = []
        self._board_size_mm: Optional[Tuple[float, float]] = None
        self._bbox_origin: Tuple[float, float] = (0.0, 0.0)
        self._drill_holes: List[DrillHole] = []

        # Render thread state
        self._r_thread: Optional[QThread] = None
        self._r_worker: Optional[RenderWorker] = None

        # 3D thread state
        self._3d_thread: Optional[QThread] = None
        self._3d_worker: Optional[ThreeDWorker] = None
        self._3d_dirty: bool = False

        self._build_ui()
        self._apply_dark_theme()

    # ── UI ────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        splitter.addWidget(self._build_left_panel())

        self._tabs = QTabWidget()
        self._viewer_2d = Viewer2D()
        self._viewer_3d = Viewer3D()
        self._tabs.addTab(self._viewer_2d, "2D Layers")
        self._tabs.addTab(self._viewer_3d, "3D View")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        splitter.addWidget(self._tabs)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 1040])

        # Status bar with embedded progress bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setFixedWidth(220)
        self._progress.setFixedHeight(16)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%v / %m layers")
        self._progress.hide()
        self._status.addPermanentWidget(self._progress)

        self._status.showMessage("Drop a RAR / ZIP file to open a PCB project.")

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(240)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        btn_open = QPushButton("Open RAR / ZIP…")
        btn_open.clicked.connect(self._open_dialog)
        layout.addWidget(btn_open)

        self._btn_close = QPushButton("Close Project")
        self._btn_close.clicked.connect(self._close_project)
        self._btn_close.setEnabled(False)
        self._btn_close.setStyleSheet(
            "QPushButton:enabled { color:#ff7070; border-color:#884444; }"
        )
        layout.addWidget(self._btn_close)

        hint = QLabel("  or drop file onto this window")
        hint.setStyleSheet("color:#666; font-size:11px;")
        layout.addWidget(hint)

        grp = QGroupBox("Layers")
        grp_layout = QVBoxLayout(grp)
        grp_layout.setContentsMargins(0, 4, 0, 4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        self._layer_container = QWidget()
        self._layer_layout = QVBoxLayout(self._layer_container)
        self._layer_layout.setContentsMargins(4, 0, 4, 0)
        self._layer_layout.setSpacing(1)
        self._layer_layout.addStretch()

        scroll.setWidget(self._layer_container)
        grp_layout.addWidget(scroll)
        layout.addWidget(grp, stretch=1)

        # All / None toggles
        row = QHBoxLayout()
        btn_all = QPushButton("All")
        btn_none = QPushButton("None")
        btn_all.clicked.connect(self._show_all)
        btn_none.clicked.connect(self._show_none)
        btn_all.setFixedHeight(24)
        btn_none.setFixedHeight(24)
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        layout.addLayout(row)

        # DPI + re-render
        dpi_row = QHBoxLayout()
        dpi_lbl = QLabel("DPI:")
        dpi_lbl.setStyleSheet("color:#ccc;")
        self._dpi_spin = QSpinBox()
        self._dpi_spin.setRange(72, 1200)
        self._dpi_spin.setValue(self._dpi)
        self._dpi_spin.setSingleStep(50)
        dpi_row.addWidget(dpi_lbl)
        dpi_row.addWidget(self._dpi_spin)
        layout.addLayout(dpi_row)

        btn_rerender = QPushButton("Re-render")
        btn_rerender.clicked.connect(self._rerender)
        btn_rerender.setFixedHeight(26)
        layout.addWidget(btn_rerender)

        return panel

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#1e1e1e; color:#ddd; }
            QGroupBox { color:#aaa; border:1px solid #444; border-radius:4px;
                        margin-top:8px; padding-top:4px; }
            QGroupBox::title { subcontrol-origin:margin; left:8px; color:#aaa; }
            QPushButton { background:#2d2d2d; color:#ddd; border:1px solid #555;
                          border-radius:3px; padding:4px 8px; }
            QPushButton:hover { background:#3a3a3a; }
            QPushButton:pressed { background:#444; }
            QTabWidget::pane { border:1px solid #444; }
            QTabBar::tab { background:#2d2d2d; color:#aaa; padding:6px 16px;
                           border:1px solid #444; }
            QTabBar::tab:selected { background:#3a3a3a; color:#fff; }
            QScrollBar:vertical { background:#2a2a2a; width:8px; }
            QScrollBar::handle:vertical { background:#555; border-radius:4px; }
            QSpinBox { background:#2d2d2d; color:#ddd; border:1px solid #555;
                       border-radius:3px; padding:2px; }
            QSplitter::handle { background:#333; }
            QStatusBar { background:#161616; color:#888; }
            QProgressBar { background:#2a2a2a; border:1px solid #555;
                           border-radius:3px; text-align:center; color:#ccc; }
            QProgressBar::chunk { background:#3a7dc9; border-radius:2px; }
        """)

    # ── Drag-drop / file open ──────────────────

    def _open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PCB Archive", os.path.expanduser("~"),
            "Archives (*.rar *.zip *.7z);;All files (*.*)",
        )
        if path:
            self._load_archive(path)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isfile(path):
                self._load_archive(path)
                break

    # ── Load pipeline ──────────────────────────

    def _load_archive(self, path: str):
        self._cancel_all()
        self._clear_state()
        self.setWindowTitle(f"Gerber PCB Viewer — {os.path.basename(path)}")
        self._status.showMessage("Extracting archive…")

        try:
            extract_dir = extract_archive(path)
        except Exception as e:
            QMessageBox.critical(self, "Extraction Failed", str(e))
            return

        layers = discover_layers(extract_dir)
        if not layers:
            QMessageBox.warning(self, "No Layers Found",
                "No Gerber files were found in the archive.")
            return

        self._layers = layers
        self._visible = {l.filename: True for l in layers}
        self._build_layer_panel()
        self._btn_close.setEnabled(True)

        # Find DRR report (gives per-file layer-pair info for blind/buried vias)
        drr_paths = [
            os.path.join(root, f)
            for root, _, files in os.walk(extract_dir)
            for f in files if f.upper().endswith(".DRR")
        ]
        layer_pairs = parse_drr(drr_paths[0]) if drr_paths else {}

        # Parse all drill TX/TXT files — layer_pairs tags each hole's span
        drill_paths = [
            l.path for l in layers
            if l.layer_type == LayerType.DRILL
            and l.extension.upper() in (".TXT", ".TX1", ".TX2", ".TX3", ".TX4",
                                         ".TX5", ".TX6", ".TX7", ".TX8")
        ]
        self._drill_holes = parse_drill_files(drill_paths, layer_pairs)

        self._dpi = self._dpi_spin.value()
        self._status.showMessage(
            f"Found {len(layers)} layers, {len(self._drill_holes)} drill holes"
            f" — rendering at {self._dpi} DPI…"
        )
        self._start_2d_render(layers)

    def _build_layer_panel(self):
        for row in self._layer_rows:
            self._layer_layout.removeWidget(row)
            row.deleteLater()
        self._layer_rows.clear()

        # Remove trailing stretch
        item = self._layer_layout.takeAt(self._layer_layout.count() - 1)

        for info in self._layers:
            row = LayerRow(info)
            row.toggled.connect(self._on_layer_toggled)
            row.color_changed.connect(self._on_layer_color_changed)
            self._layer_layout.addWidget(row)
            self._layer_rows.append(row)

        self._layer_layout.addStretch()

    # ── 2D render thread ───────────────────────

    def _start_2d_render(self, layers: List[LayerInfo]):
        worker = RenderWorker(layers, self._dpi)
        thread = QThread()
        worker.moveToThread(thread)
        worker.layer_done.connect(self._on_layer_done)
        worker.progress.connect(self._on_2d_progress)
        worker.finished.connect(self._on_2d_finished)
        thread.started.connect(worker.run)
        self._r_worker = worker
        self._r_thread = thread

        self._progress.setRange(0, len(layers))
        self._progress.setValue(0)
        self._progress.show()

        thread.start()

    def _on_layer_done(self, result):
        if result is not None:
            self._rendered.append(result)
        n = len(self._rendered)
        if n % 3 == 0 or n == 1:
            self._refresh_2d()

    def _on_2d_progress(self, done: int, total: int):
        self._progress.setRange(0, total)
        self._progress.setValue(done)
        self._status.showMessage(f"Rendering layers… {done} / {total}")

    def _on_2d_finished(self):
        if self._r_thread:
            self._r_thread.quit()
            self._r_thread.wait()
        self._r_thread = None
        self._r_worker = None
        self._refresh_2d()
        self._3d_dirty = True
        self._progress.hide()

        n = len(self._rendered)
        w, h = get_board_size_mm(self._rendered)
        self._board_size_mm = (w, h)
        self._status.showMessage(
            f"Loaded {n} layers  |  Board: {w:.1f} × {h:.1f} mm  |  DPI: {self._dpi}"
        )

        # If user is already on the 3D tab, kick off 3D render now
        if self._tabs.currentIndex() == 1:
            self._start_3d_render()

    # ── 3D render thread ───────────────────────

    def _on_tab_changed(self, index: int):
        if index == 1 and self._3d_dirty and self._rendered:
            self._start_3d_render()

    def _start_3d_render(self):
        if self._3d_thread and self._3d_thread.isRunning():
            return  # already running
        if not self._rendered:
            return

        self._viewer_3d.set_loading(True)
        self._status.showMessage("Building 3D view…")

        n = len(self._rendered)
        self._progress.setRange(0, n)
        self._progress.setValue(0)
        self._progress.show()

        worker = ThreeDWorker(list(self._rendered), self._dpi)
        thread = QThread()
        worker.moveToThread(thread)
        worker.progress.connect(self._on_3d_progress)
        worker.ready.connect(self._on_3d_ready)
        thread.started.connect(worker.run)
        self._3d_worker = worker
        self._3d_thread = thread
        thread.start()

    def _on_3d_progress(self, done: int, total: int):
        self._progress.setRange(0, total)
        self._progress.setValue(done)
        self._viewer_3d.show_progress(done, total)
        self._status.showMessage(f"Building 3D view… {done} / {total} layers")

    def _on_3d_ready(self, layer_images: dict):
        if self._3d_thread:
            self._3d_thread.quit()
            self._3d_thread.wait()
        self._3d_thread = None
        self._3d_worker = None
        self._progress.hide()
        self._viewer_3d.hide_progress()

        bsz = self._board_size_mm or get_board_size_mm(self._rendered)
        gmin_x, _, gmin_y, _ = get_global_bbox(self._rendered)
        self._bbox_origin = (gmin_x, gmin_y)

        self._viewer_3d.update_layer_images(layer_images, bsz, dict(self._visible))

        # Pass drill holes for via rendering
        if self._drill_holes:
            self._viewer_3d.set_vias(self._drill_holes, gmin_x, gmin_y)

        self._3d_dirty = False

        n = len(self._rendered)
        w, h = bsz
        v = len(self._drill_holes)
        self._status.showMessage(
            f"3D ready  |  {n} layers  |  {v} vias  |  Board: {w:.1f} × {h:.1f} mm"
        )

    # ── Compositing helpers ────────────────────

    def _refresh_2d(self):
        if not self._rendered:
            return
        img = composite_layers(
            self._rendered, self._visible, self._dpi,
            color_overrides=self._layer_colors or None,
        )
        bsz = get_board_size_mm(self._rendered)
        self._viewer_2d.update_image(img, bsz)

    # ── Layer panel events ─────────────────────

    def _on_layer_toggled(self, filename: str, visible: bool):
        self._visible[filename] = visible
        self._refresh_2d()
        self._viewer_3d.toggle_layer(filename, visible)

    def _on_layer_color_changed(self, filename: str, r: int, g: int, b: int):
        self._layer_colors[filename] = (r, g, b)
        self._refresh_2d()
        self._viewer_3d.update_layer_color(filename, r, g, b)

    def _show_all(self):
        self._visible = {k: True for k in self._visible}
        for row in self._layer_rows:
            row._check.blockSignals(True)
            row._check.setChecked(True)
            row._check.blockSignals(False)
        self._refresh_2d()
        self._viewer_3d.set_all_visible(True)

    def _show_none(self):
        self._visible = {k: False for k in self._visible}
        for row in self._layer_rows:
            row._check.blockSignals(True)
            row._check.setChecked(False)
            row._check.blockSignals(False)
        self._refresh_2d()
        self._viewer_3d.set_all_visible(False)

    def _rerender(self):
        if not self._layers:
            return
        self._cancel_all()
        self._rendered.clear()
        self._3d_dirty = False
        self._viewer_2d.clear()
        self._viewer_3d.clear()
        for l in self._layers:
            l.rendered = False
            l.failed = False
        self._dpi = self._dpi_spin.value()
        self._start_2d_render(self._layers)

    # ── Cleanup ────────────────────────────────

    def _cancel_all(self):
        for worker, thread in [
            (self._r_worker, self._r_thread),
            (self._3d_worker, self._3d_thread),
        ]:
            if worker:
                worker.cancel()
            if thread and thread.isRunning():
                thread.quit()
                thread.wait()
        self._r_worker = self._r_thread = None
        self._3d_worker = self._3d_thread = None

    def _close_project(self):
        self._cancel_all()
        self._clear_state()
        self.setWindowTitle("Gerber PCB Viewer")
        self._status.showMessage("Drop a RAR / ZIP file to open a PCB project.")

    def _clear_state(self):
        self._layers.clear()
        self._rendered.clear()
        self._visible.clear()
        self._layer_colors.clear()
        self._drill_holes.clear()
        self._board_size_mm = None
        self._bbox_origin = (0.0, 0.0)
        self._3d_dirty = False
        self._btn_close.setEnabled(False)
        for row in self._layer_rows:
            self._layer_layout.removeWidget(row)
            row.deleteLater()
        self._layer_rows.clear()
        while self._layer_layout.count():
            self._layer_layout.takeAt(0)
        self._layer_layout.addStretch()
        self._viewer_2d.clear()
        self._viewer_3d.clear()
        self._progress.hide()

    def closeEvent(self, event):
        self._cancel_all()
        super().closeEvent(event)
