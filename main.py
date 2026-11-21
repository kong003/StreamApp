"""StreamApp – mehrere RTMP-Streams mit unterschiedlichen Sprachmixen."""
import sys

from PySide6.QtWidgets import QApplication

from streamapp.config import load_config
from streamapp.ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("StreamApp")
    app.setStyle("Fusion")
    win = MainWindow(load_config())
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
