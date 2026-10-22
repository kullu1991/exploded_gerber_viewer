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
    app.setApplicationVersion("1.0.0")

    win = MainWindow()
    win.show()
    # Deferred maximise — required in frozen exe where the window manager
    # hasn't processed the initial show event yet.
    QTimer.singleShot(100, win.showMaximized)

    # Open an archive given on the command line ("Open with…" / drag onto the EXE).
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        path = sys.argv[1]
        QTimer.singleShot(300, lambda: win._load_archive(path))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
