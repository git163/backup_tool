"""
对话框模块
- PasswordDialog: SSH 密码输入
- RemoteDirDialog: SSH 远程目录浏览器（异步加载）
- ConfirmDialog: Markdown 确认对话框
"""

from gui.qt_compat import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QMessageBox,
    QTextBrowser, QCheckBox, QProgressBar, QThread, Signal,
    Qt, QCursor, QApplication
)

from lib.fs import RemoteFS, parse_path
from lib.ssh_client import SSHPool, AuthenticationError


class PasswordDialog(QDialog):
    """SSH 密码输入对话框。"""

    def __init__(self, user_host: str, parent=None):
        super().__init__(parent)
        self.user_host = user_host
        self.setWindowTitle("SSH Password")
        self.setMinimumWidth(300)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(f"Enter password for {self.user_host}"))

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("Password")
        layout.addWidget(self.password_edit)

        self.remember_checkbox = QCheckBox("Remember password")
        layout.addWidget(self.remember_checkbox)

        btn_layout = QHBoxLayout()
        self.ok_btn = QPushButton("OK")
        self.cancel_btn = QPushButton("Cancel")
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

    def get_password(self) -> tuple[str, bool]:
        """返回 (password, remember)。"""
        return self.password_edit.text(), self.remember_checkbox.isChecked()


class _RemoteDirLoader(QThread):
    """异步加载远程目录列表。"""
    loaded = Signal(list)
    error = Signal(str)

    def __init__(self, ssh_pool: SSHPool, user_host: str, password: str, path: str):
        super().__init__()
        self.ssh_pool = ssh_pool
        self.user_host = user_host
        self.password = password
        self.path = path

    def run(self):
        try:
            conn = self.ssh_pool.get(self.user_host, self.password)
            fs = RemoteFS(conn)
            entries = []
            for name in fs.listdir(self.path):
                full_path = fs.join(self.path, name)
                is_dir = fs.isdir(full_path)
                entries.append((name, is_dir, full_path))
            self.loaded.emit(entries)
        except Exception as e:
            self.error.emit(str(e))


class RemoteDirDialog(QDialog):
    """SSH 远程目录浏览器。"""

    def __init__(self, ssh_pool: SSHPool, user_host: str, password: str, initial_path: str = "/", parent=None):
        super().__init__(parent)
        self.ssh_pool = ssh_pool
        self.user_host = user_host
        self.password = password
        self.current_path = "/"
        self.selected_path = ""
        self.setWindowTitle(f"Remote Browser - {user_host}")
        self.setMinimumSize(500, 400)
        self._setup_ui()
        self._load_dir(initial_path if initial_path else "/")

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.path_label = QLabel("/")
        layout.addWidget(self.path_label)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.list_widget)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        btn_layout = QHBoxLayout()
        self.up_btn = QPushButton("Up")
        self.refresh_btn = QPushButton("Refresh")
        self.select_btn = QPushButton("Select")
        self.cancel_btn = QPushButton("Cancel")

        self.up_btn.clicked.connect(self._go_up)
        self.refresh_btn.clicked.connect(self._refresh)
        self.select_btn.clicked.connect(self._on_select)
        self.cancel_btn.clicked.connect(self.reject)

        btn_layout.addWidget(self.up_btn)
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.select_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

    def _load_dir(self, path: str):
        self.current_path = path
        self.path_label.setText(path)
        self.list_widget.clear()
        self.status_label.setText("Loading...")
        self.setEnabled(False)
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))

        self.loader = _RemoteDirLoader(self.ssh_pool, self.user_host, self.password, path)
        self.loader.loaded.connect(self._on_loaded)
        self.loader.error.connect(self._on_load_error)
        self.loader.finished.connect(self._on_load_finished)
        self.loader.start()

    def _on_loaded(self, entries):
        self.list_widget.clear()
        # 目录排在前面
        dirs = [(name, p) for name, is_dir, p in entries if is_dir]
        files = [(name, p) for name, is_dir, p in entries if not is_dir]
        for name, p in dirs:
            item = QListWidgetItem(f"[D] {name}")
            item.setData(Qt.UserRole, p)
            self.list_widget.addItem(item)
        for name, p in files:
            item = QListWidgetItem(f"[F] {name}")
            item.setData(Qt.UserRole, p)
            self.list_widget.addItem(item)
        self.status_label.setText(f"{len(dirs)} dirs, {len(files)} files")

    def _on_load_error(self, msg):
        self.status_label.setText(f"Error: {msg}")

    def _on_load_finished(self):
        self.setEnabled(True)
        QApplication.restoreOverrideCursor()

    def _on_double_click(self, item):
        path = item.data(Qt.UserRole)
        if item.text().startswith("[D]"):
            self._load_dir(path)
        else:
            self.selected_path = path
            self.accept()

    def _go_up(self):
        if self.current_path == "/":
            return
        parent = self.current_path.rsplit("/", 1)[0] or "/"
        self._load_dir(parent)

    def _refresh(self):
        self._load_dir(self.current_path)

    def _on_select(self):
        item = self.list_widget.currentItem()
        if item:
            self.selected_path = item.data(Qt.UserRole)
        else:
            self.selected_path = self.current_path
        self.accept()


class ConfirmDialog(QDialog):
    """Markdown/HTML 确认对话框。"""

    def __init__(self, title: str, markdown_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(500, 400)
        self._setup_ui(markdown_text)

    def _setup_ui(self, text):
        layout = QVBoxLayout(self)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        # 简单 Markdown 转 HTML
        html = self._markdown_to_html(text)
        self.browser.setHtml(html)
        layout.addWidget(self.browser)

        btn_layout = QHBoxLayout()
        self.yes_btn = QPushButton("Yes")
        self.no_btn = QPushButton("No")
        self.yes_btn.clicked.connect(self.accept)
        self.no_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(self.yes_btn)
        btn_layout.addWidget(self.no_btn)
        layout.addLayout(btn_layout)

    def _markdown_to_html(self, text: str) -> str:
        """简单 Markdown 转 HTML。"""
        lines = text.split("\n")
        html_lines = ["<html><body style='font-family:sans-serif;'>"]
        in_table = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# "):
                html_lines.append(f"<h1>{stripped[2:]}</h1>")
            elif stripped.startswith("## "):
                html_lines.append(f"<h2>{stripped[3:]}</h2>")
            elif stripped.startswith("- "):
                html_lines.append(f"<li>{stripped[2:]}</li>")
            elif "|" in stripped:
                if not in_table:
                    html_lines.append("<table border='1' cellpadding='4' cellspacing='0'>")
                    in_table = True
                # 跳过分隔行
                if "---" in stripped or "====" in stripped:
                    continue
                cells = [c.strip() for c in stripped.split("|") if c.strip()]
                row = "".join(f"<td>{c}</td>" for c in cells)
                html_lines.append(f"<tr>{row}</tr>")
            else:
                if in_table:
                    html_lines.append("</table>")
                    in_table = False
                html_lines.append(f"<p>{stripped}</p>")
        if in_table:
            html_lines.append("</table>")
        html_lines.append("</body></html>")
        return "\n".join(html_lines)
