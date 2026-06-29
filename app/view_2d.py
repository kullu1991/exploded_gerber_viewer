from PyQt6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QWidget, QVBoxLayout, QHBoxLayout, QToolBar, QPushButton, QLabel,
    QProgressBar,
)
from PyQt6.QtGui import QPixmap, QImage, QWheelEvent, QMouseEvent, QTransform
from PyQt6.QtCore import Qt, QPoint

from PIL import Image
import numpy as np


def pil_to_qpixmap(image: Image.Image) -> QPixmap:
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    data = image.tobytes("raw", "RGBA")
    qimg = QImage(data, image.width, image.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg)


class PCBGraphicsView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = QGraphicsPixmapItem()
        self._scene.addItem(self._pixmap_item)

        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(self.renderHints())
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self._zoom = 1.0

    def set_image(self, image: Image.Image):
        px = pil_to_qpixmap(image)
        self._pixmap_item.setPixmap(px)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())

    def fit_view(self):
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        t = self.transform()
        self._zoom = t.m11()

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom *= factor
        self.scale(factor, factor)

    def zoom_in(self):
        self.scale(1.25, 1.25)
        self._zoom *= 1.25

    def zoom_out(self):
        self.scale(1 / 1.25, 1 / 1.25)
        self._zoom /= 1.25

    def zoom_level(self) -> float:
        return self._zoom


class Viewer2D(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: Image.Image | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar
        toolbar = QToolBar()
        btn_fit = QPushButton("Fit")
        btn_zoom_in = QPushButton("+")
        btn_zoom_out = QPushButton("−")
        self._lbl_zoom = QLabel("100%")
        self._lbl_size = QLabel("")

        btn_fit.setFixedWidth(40)
        btn_zoom_in.setFixedWidth(30)
        btn_zoom_out.setFixedWidth(30)
        self._lbl_zoom.setFixedWidth(55)

        toolbar.addWidget(btn_fit)
        toolbar.addWidget(btn_zoom_out)
        toolbar.addWidget(self._lbl_zoom)
        toolbar.addWidget(btn_zoom_in)
        toolbar.addSeparator()
        toolbar.addWidget(self._lbl_size)

        self._view = PCBGraphicsView()

        # In-view progress bar (shown while layers render)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.setFormat("Rendering layers… %v / %m")
        self._progress.setFixedHeight(18)
        self._progress.setStyleSheet(
            "QProgressBar{background:#1a1a2a;border:none;color:#aaa;font-size:11px;}"
            "QProgressBar::chunk{background:#3a7dc9;}"
        )
        self._progress.hide()

        layout.addWidget(toolbar)
        layout.addWidget(self._view)
        layout.addWidget(self._progress)

        btn_fit.clicked.connect(self._fit)
        btn_zoom_in.clicked.connect(self._zoom_in)
        btn_zoom_out.clicked.connect(self._zoom_out)

    def show_progress(self, done: int, total: int):
        self._progress.setRange(0, total)
        self._progress.setValue(done)
        self._progress.show()

    def hide_progress(self):
        self._progress.hide()

    def update_image(self, image: Image.Image, board_size_mm=None):
        self._image = image
        self._view.set_image(image)
        self._view.fit_view()
        self._update_labels()
        if board_size_mm:
            w, h = board_size_mm
            self._lbl_size.setText(f"  {w:.1f} × {h:.1f} mm")

    def _fit(self):
        self._view.fit_view()
        self._update_labels()

    def _zoom_in(self):
        self._view.zoom_in()
        self._update_labels()

    def _zoom_out(self):
        self._view.zoom_out()
        self._update_labels()

    def _update_labels(self):
        pct = int(self._view.zoom_level() * 100)
        self._lbl_zoom.setText(f"{pct}%")

    def clear(self):
        self._view._pixmap_item.setPixmap(QPixmap())
        self._lbl_size.setText("")
