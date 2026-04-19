"""
主窗口模块
- 三个目录输入框（Backup/Output/Target）
- 浏览按钮（本地文件对话框 + 远程目录浏览器）
- Patch / Rollback 操作按钮
- 实时日志窗口
- 配置保存/加载
- 后台线程管理
"""

import os
import sys

from gui.qt_compat import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QComboBox,
    QFileDialog, QMessageBox, QApplication, Qt, QCursor,
    QMenuBar, QMenu, QAction
)
from gui.dialogs import PasswordDialog, RemoteDirDialog, ConfirmDialog
from gui.thread import PreCheckThread, WorkerThread
from lib.config import Config
from lib.fs import parse_path, LocalFS
from lib.ssh_client import SSHPool, AuthenticationError
from lib.compat import CompatStatus
from lib.logger import AppLogger, QtLogHandler


class MainWindow(QMainWindow):
    def __init__(self, config_path: str = "conf/config.json"):
        super().__init__()
        self.config_path = config_path
        self.config = Config()
        if os.path.exists(config_path):
            self.config.load(config_path)

        self.logger = AppLogger.setup()
        self.qt_handler = QtLogHandler()
        self.qt_handler.log_signal.connect(self._on_log)
        self.logger.addHandler(self.qt_handler)

        self.ssh_pool = SSHPool()
        self.current_thread = None

        self.setWindowTitle("Auto Backup and Patch Tool")
        self.setMinimumSize(700, 500)
        self._setup_ui()
        self._load_defaults()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # 目录输入区
        grid = QGridLayout()
        self.backup_edit = QLineEdit()
        self.output_edit = QLineEdit()
        self.target_edit = QLineEdit()

        self.backup_btn = QPushButton("Browse")
        self.output_btn = QPushButton("Browse")
        self.target_btn = QPushButton("Browse")

        self.backup_btn.clicked.connect(lambda: self._browse(self.backup_edit))
        self.output_btn.clicked.connect(lambda: self._browse(self.output_edit))
        self.target_btn.clicked.connect(lambda: self._browse(self.target_edit))

        grid.addWidget(QLabel("Backup Dir:"), 0, 0)
        grid.addWidget(self.backup_edit, 0, 1)
        grid.addWidget(self.backup_btn, 0, 2)

        grid.addWidget(QLabel("Output Dir:"), 1, 0)
        grid.addWidget(self.output_edit, 1, 1)
        grid.addWidget(self.output_btn, 1, 2)

        grid.addWidget(QLabel("Target Dir:"), 2, 0)
        grid.addWidget(self.target_edit, 2, 1)
        grid.addWidget(self.target_btn, 2, 2)

        main_layout.addLayout(grid)

        # 回滚备份选择
        rollback_layout = QHBoxLayout()
        rollback_layout.addWidget(QLabel("Rollback Backup:"))
        self.rollback_combo = QComboBox()
        self.rollback_combo.setEnabled(False)
        rollback_layout.addWidget(self.rollback_combo)
        self.refresh_backup_btn = QPushButton("Refresh")
        self.refresh_backup_btn.clicked.connect(self._refresh_backups)
        rollback_layout.addWidget(self.refresh_backup_btn)
        rollback_layout.addStretch()
        main_layout.addLayout(rollback_layout)

        # 操作按钮 + 配置按钮
        btn_layout = QHBoxLayout()
        self.patch_btn = QPushButton("Patch")
        self.rollback_btn = QPushButton("Rollback")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.save_config_btn = QPushButton("Save Config")
        self.load_config_btn = QPushButton("Load Config")

        self.patch_btn.clicked.connect(self._on_patch)
        self.rollback_btn.clicked.connect(self._on_rollback)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.save_config_btn.clicked.connect(self._save_config)
        self.load_config_btn.clicked.connect(self._load_config)

        btn_layout.addWidget(self.patch_btn)
        btn_layout.addWidget(self.rollback_btn)
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.save_config_btn)
        btn_layout.addWidget(self.load_config_btn)
        main_layout.addLayout(btn_layout)

        # 日志区
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        main_layout.addWidget(self.log_edit)

        # 菜单栏
        self._setup_menu()

    def _setup_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")

        save_action = QAction("Save Config", self)
        save_action.triggered.connect(self._save_config)
        file_menu.addAction(save_action)

        load_action = QAction("Load Config", self)
        load_action.triggered.connect(self._load_config)
        file_menu.addAction(load_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def _load_defaults(self):
        self.backup_edit.setText(self.config.get("backup", ""))
        self.output_edit.setText(self.config.get("output", ""))
        self.target_edit.setText(self.config.get("target", ""))

    def _browse(self, edit: QLineEdit):
        path = edit.text().strip()
        is_remote, user, host, real_path = parse_path(path)

        if is_remote:
            user_host = f"{user}@{host}"
            password = self.config.ssh_passwords.get(user_host)
            if not password:
                password = self._ask_password(user_host)
                if not password:
                    return
            dialog = RemoteDirDialog(self.ssh_pool, user_host, password, self)
            if dialog.exec_() == QDialog.Accepted:
                edit.setText(f"{user_host}:{dialog.selected_path}")
        else:
            directory = QFileDialog.getExistingDirectory(self, "Select Directory", real_path or "")
            if directory:
                edit.setText(directory)

    def _ask_password(self, user_host: str) -> str:
        dialog = PasswordDialog(user_host, self)
        if dialog.exec_() == QDialog.Accepted:
            password, remember = dialog.get_password()
            if remember:
                self.config.set_ssh_password(user_host, password)
            # 验证密码
            try:
                conn = self.ssh_pool.get(user_host, password)
                if conn.verify_password(password):
                    return password
            except Exception:
                pass
            QMessageBox.warning(self, "Auth Failed", f"Failed to authenticate {user_host}")
        return ""

    def _on_patch(self):
        output = self.output_edit.text().strip()
        target = self.target_edit.text().strip()
        backup = self.backup_edit.text().strip()

        if not output or not target:
            QMessageBox.warning(self, "Input Error", "Output and Target directories are required")
            return

        is_remote, _, _, real_output = parse_path(output)
        if not is_remote and not os.path.exists(real_output):
            QMessageBox.warning(self, "Input Error", f"Output directory not found: {output}")
            return

        self._set_busy(True)
        self._run_precheck(output, target, lambda status, overlapping: self._on_precheck_done(
            status, overlapping, 'patch', output, target, backup
        ))

    def _on_rollback(self):
        backup = self.backup_edit.text().strip()
        target = self.target_edit.text().strip()

        if not backup or not target:
            QMessageBox.warning(self, "Input Error", "Backup and Target directories are required")
            return

        backup_path = self.rollback_combo.currentData()
        if not backup_path:
            QMessageBox.warning(self, "Input Error", "Please select a backup version")
            return

        self._set_busy(True)
        self._run_precheck(backup_path, target, lambda status, overlapping: self._on_precheck_done(
            status, overlapping, 'rollback', backup_path, target, backup
        ))

    def _run_precheck(self, source_path: str, target_path: str, callback):
        self.current_thread = PreCheckThread(source_path, target_path, self.ssh_pool, self.config)
        self.current_thread.result.connect(lambda status, overlapping: callback(status, overlapping))
        self.current_thread.error.connect(self._on_thread_error)
        self.current_thread.log.connect(self._on_log)
        self.current_thread.finished.connect(self._on_thread_finished)
        self.current_thread.start()

    def _on_precheck_done(self, status: str, overlapping: list, op_type: str, source: str, target: str, backup: str):
        if status == CompatStatus.NONE.value:
            QMessageBox.critical(self, "Incompatible", "Source and target have no overlap. Operation aborted.")
            self._set_busy(False)
            return

        if status == CompatStatus.PARTIAL.value:
            diff_text = self._build_diff_text(overlapping)
            dialog = ConfirmDialog("Partial Match", diff_text, self)
            if dialog.exec_() != QDialog.Accepted:
                self._set_busy(False)
                return

        if overlapping:
            overlap_text = "The following items will be overwritten:\n\n"
            for name in overlapping[:20]:
                overlap_text += f"- {name}\n"
            if len(overlapping) > 20:
                overlap_text += f"... and {len(overlapping) - 20} more\n"
            overlap_text += "\nContinue?"
            dialog = ConfirmDialog("Confirm Overwrite", overlap_text, self)
            if dialog.exec_() != QDialog.Accepted:
                self._set_busy(False)
                return

        # 启动工作线程
        self._run_worker(op_type, source, target, backup)

    def _build_diff_text(self, overlapping: list) -> str:
        text = "## Partial Match Detected\n\n"
        text += "The following items exist in both directories:\n\n"
        for name in overlapping[:10]:
            text += f"- {name}\n"
        if len(overlapping) > 10:
            text += f"- ... and {len(overlapping) - 10} more\n"
        text += "\nDo you want to continue?"
        return text

    def _run_worker(self, op_type: str, source: str, target: str, backup: str):
        paths = {'output': source, 'target': target, 'backup': backup}
        if op_type == 'rollback':
            paths = {'backup': source, 'target': target, 'backup_dir': backup}

        self.current_thread = WorkerThread(op_type, paths, self.ssh_pool, self.config)
        self.current_thread.progress.connect(self._on_progress)
        self.current_thread.log.connect(self._on_log)
        self.current_thread.finished_sig.connect(self._on_worker_finished)
        self.current_thread.error.connect(self._on_thread_error)
        self.current_thread.start()

    def _on_worker_finished(self, success: bool, message: str):
        self._set_busy(False)
        if success:
            QMessageBox.information(self, "Success", message)
        else:
            QMessageBox.critical(self, "Failed", message)

    def _on_thread_error(self, msg: str):
        self._set_busy(False)
        if msg.startswith("AUTH:"):
            # 需要密码
            user_host = msg.split(":", 1)[1].strip()
            password = self._ask_password(user_host)
            if password:
                # 重试
                self.logger.info(f"Retrying with new password for {user_host}")
                # 触发原来的操作重新执行
                # 这里简化处理，让用户手动重新点击
        else:
            QMessageBox.critical(self, "Error", msg)

    def _on_thread_finished(self):
        pass

    def _on_cancel(self):
        if self.current_thread and hasattr(self.current_thread, 'cancel'):
            self.current_thread.cancel()
            self._on_log("Cancelling operation...")

    def _on_progress(self, step: str, detail: str):
        self._on_log(f"[{step}] {detail}")

    def _on_log(self, msg: str):
        self.log_edit.append(msg)
        # 滚动到底部
        scrollbar = self.log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_busy(self, busy: bool):
        self.patch_btn.setEnabled(not busy)
        self.rollback_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.backup_btn.setEnabled(not busy)
        self.output_btn.setEnabled(not busy)
        self.target_btn.setEnabled(not busy)
        self.refresh_backup_btn.setEnabled(not busy)
        self.save_config_btn.setEnabled(not busy)
        self.load_config_btn.setEnabled(not busy)
        if busy:
            QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        else:
            QApplication.restoreOverrideCursor()

    def _refresh_backups(self):
        backup_dir = self.backup_edit.text().strip()
        if not backup_dir:
            return
        self.rollback_combo.clear()
        self.rollback_combo.setEnabled(False)

        try:
            is_remote, user, host, real_path = parse_path(backup_dir)
            if is_remote:
                user_host = f"{user}@{host}"
                password = self.config.ssh_passwords.get(user_host)
                if not password:
                    password = self._ask_password(user_host)
                    if not password:
                        return
                from lib.fs import RemoteFS
                conn = self.ssh_pool.get(user_host, password)
                fs = RemoteFS(conn)
                entries = fs.listdir(real_path)
            else:
                fs = LocalFS()
                entries = fs.listdir(real_path)

            # 过滤带时间戳的备份目录
            import re
            backups = []
            for name in entries:
                if re.match(r'.*_\d{8}_\d{6}$', name):
                    backups.append(name)
            backups.sort(reverse=True)

            for name in backups:
                full_path = fs.join(real_path, name)
                display = f"{name}"
                self.rollback_combo.addItem(display, full_path)

            if backups:
                self.rollback_combo.setEnabled(True)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to list backups: {e}")

    def _save_config(self):
        self.config.set("backup", self.backup_edit.text().strip())
        self.config.set("output", self.output_edit.text().strip())
        self.config.set("target", self.target_edit.text().strip())
        self.config.save(self.config_path)
        self._on_log(f"Config saved to {self.config_path}")

    def _load_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Config", "conf", "JSON (*.json)")
        if path:
            self.config.load(path)
            self._load_defaults()
            self._on_log(f"Config loaded from {path}")

    def closeEvent(self, event):
        self.ssh_pool.clear_all()
        event.accept()
