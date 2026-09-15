"""Qt application entry point."""

import multiprocessing
import os
import sys

import pyqtgraph as pg
from PyQt6.QtWidgets import QApplication

from .window import MDFPlotter


def main():
    multiprocessing.freeze_support()
    # The baseline contains emoji diagnostics.  On Chinese Windows consoles
    # stdout is commonly GBK; replacement keeps cleanup from being interrupted.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    pg.setConfigOptions(antialias=True)
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--enable-local-file-accesses --disable-web-security",
    )
    app = QApplication.instance() or QApplication(sys.argv)
    window = MDFPlotter()
    window.show()
    return app.exec()
