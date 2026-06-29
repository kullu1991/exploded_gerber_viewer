import os
import sys

# Must be set before qtpy/__init__.py runs so it selects PyQt6
os.environ["QT_API"] = "pyqt6"
os.environ["FORCE_QT_API"] = "1"

# Pre-load PyQt6 core modules so qtpy's sys.modules check succeeds
# even if the packaging.version import fails for any reason.
try:
    import PyQt6.QtCore   # noqa: F401
    import PyQt6.QtGui    # noqa: F401
    import PyQt6.QtWidgets  # noqa: F401
except Exception:
    pass
