import os
import sys

# Must be set before any Qt imports so pyvistaqt picks up PyQt6
os.environ["QT_API"] = "pyqt6"

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from app.window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Gerber PCB Viewer")
    app.setApplicationVersion("1.0")

    win = MainWindow()
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
