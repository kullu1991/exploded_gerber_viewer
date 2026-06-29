import os
import sys

# Must be set before any Qt imports so pyvistaqt picks up PyQt6
os.environ["QT_API"] = "pyqt6"

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer

from app.window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Gerber PCB Viewer")
    app.setApplicationVersion("1.0")

    win = MainWindow()
    win.show()
    # Deferred maximise — required in frozen exe where the window manager
    # hasn't processed the initial show event yet.
    QTimer.singleShot(100, win.showMaximized)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
