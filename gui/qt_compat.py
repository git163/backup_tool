"""
PySide6 / PySide2 兼容层
优先尝试 PySide6，失败则降级到 PySide2
GUI 所有文件均从此模块导入 Qt 类
"""

import sys

try:
    from PySide6.QtCore import (
        QObject, QThread, Signal, Qt, QTimer, QSettings,
        QCoreApplication, QMetaObject
    )
    from PySide6.QtGui import (
        QIcon, QPixmap, QCursor, QFont, QTextCursor, QColor,
        QAction
    )
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QDialog,
        QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
        QLabel, QLineEdit, QPushButton, QComboBox,
        QTextEdit, QTextBrowser, QListView, QTreeView,
        QFileDialog, QMessageBox, QInputDialog,
        QProgressBar, QStatusBar, QMenuBar, QMenu,
        QSplitter, QFrame, QGroupBox,
        QScrollArea, QToolButton, QCheckBox,
        QAbstractItemView, QListWidget, QListWidgetItem
    )
    PYSIDE_VERSION = 6
except ImportError:
    from PySide2.QtCore import (
        QObject, QThread, Signal, Qt, QTimer, QSettings,
        QCoreApplication, QMetaObject
    )
    from PySide2.QtGui import (
        QIcon, QPixmap, QCursor, QFont, QTextCursor, QColor
    )
    from PySide2.QtWidgets import (
        QApplication, QMainWindow, QWidget, QDialog,
        QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
        QLabel, QLineEdit, QPushButton, QComboBox,
        QTextEdit, QTextBrowser, QListView, QTreeView,
        QFileDialog, QMessageBox, QInputDialog,
        QProgressBar, QStatusBar, QMenuBar, QMenu,
        QAction, QSplitter, QFrame, QGroupBox,
        QScrollArea, QToolButton, QCheckBox,
        QAbstractItemView, QListWidget, QListWidgetItem
    )
    PYSIDE_VERSION = 2


def exec_dialog(dialog):
    """统一封装 exec / exec_ 调用"""
    if PYSIDE_VERSION >= 6:
        return dialog.exec()
    else:
        return dialog.exec_()
