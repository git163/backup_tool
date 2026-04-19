"""
Application entry point
Detects PySide6 / PySide2 and starts the main window.
"""

import sys
import os

# Add project root to PYTHONPATH
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from gui.qt_compat import QApplication
from gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Auto Backup and Patch Tool")
    app.setOrganizationName("backup_tool")

    config_path = sys.argv[1] if len(sys.argv) > 1 else "conf/config.json"
    window = MainWindow(config_path)
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
