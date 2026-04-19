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
    QLabel, QLineEdit, QPushButton, QTextEdit, QComboBox, QCheckBox,
    QFileDialog, QMessageBox, QApplication, Qt, QCursor,
    QMenuBar, QMenu, QAction, QDialog
)
from gui.dialogs import PasswordDialog, RemoteDirDialog, ConfirmDialog
from gui.thread import PreCheckThread, WorkerThread, ListBackupsThread
from lib.config import Config
from lib.fs import parse_path
from lib.ssh_client import SSHPool, AuthenticationError
from lib.compat import CompatStatus
from lib.logger import AppLogger


class _AutoRefreshComboBox(QComboBox):
    """点击展开时自动调用刷新回调"""
    def __init__(self, refresh_callback, parent=None):
        super().__init__(parent)
        self._refresh_callback = refresh_callback

    def showPopup(self):
        self._refresh_callback()
        super().showPopup()


class MainWindow(QMainWindow):
    def __init__(self, config_path: str = "conf/config.json"):
        super().__init__()
        self.config_path = config_path
        self.config = Config()
        if os.path.exists(config_path):
            self.config.load(config_path)

        self.logger = AppLogger.setup()

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
        grid.setColumnStretch(1, 1)  # 让输入框列可以拉伸，拉长编辑框
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
        backup_btn_layout = QHBoxLayout()
        backup_btn_layout.addStretch()
        backup_btn_layout.addWidget(self.backup_btn)
        grid.addLayout(backup_btn_layout, 0, 2)

        grid.addWidget(QLabel("Output Dir:"), 1, 0)
        grid.addWidget(self.output_edit, 1, 1)
        output_btn_layout = QHBoxLayout()
        output_btn_layout.addStretch()
        output_btn_layout.addWidget(self.output_btn)
        grid.addLayout(output_btn_layout, 1, 2)

        grid.addWidget(QLabel("Target Dir:"), 2, 0)
        grid.addWidget(self.target_edit, 2, 1)
        target_btn_layout = QHBoxLayout()
        self.target_via_checkbox = QCheckBox("Via Output Host")
        target_btn_layout.addWidget(self.target_via_checkbox, 1)
        target_btn_layout.addWidget(self.target_btn, 1)
        grid.addLayout(target_btn_layout, 2, 2)

        main_layout.addLayout(grid)

        # 回滚备份选择
        rollback_layout = QHBoxLayout()
        rollback_layout.addWidget(QLabel("Rollback Backup:"))
        self.rollback_combo = _AutoRefreshComboBox(self._refresh_backups)
        self.rollback_combo.setMinimumWidth(300)
        self.rollback_combo.setMaxVisibleItems(10)
        self.rollback_combo.view().setMinimumHeight(120)
        rollback_layout.addWidget(self.rollback_combo)
        rollback_layout.addStretch()
        main_layout.addLayout(rollback_layout)

        # 操作按钮 + 配置按钮
        btn_layout = QHBoxLayout()
        self.backup_btn = QPushButton("Backup")
        self.patch_btn = QPushButton("Patch")
        self.rollback_btn = QPushButton("Rollback")
        self.save_config_btn = QPushButton("Save Config")
        self.load_config_btn = QPushButton("Load Config")

        self.backup_btn.clicked.connect(self._on_backup)
        self.patch_btn.clicked.connect(self._on_patch)
        self.rollback_btn.clicked.connect(self._on_rollback)
        self.save_config_btn.clicked.connect(self._save_config)
        self.load_config_btn.clicked.connect(self._load_config)

        btn_layout.addWidget(self.backup_btn)
        btn_layout.addWidget(self.patch_btn)
        btn_layout.addWidget(self.rollback_btn)
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
        self.target_via_checkbox.setChecked(self.config.get("target_via_output", False))

    def _browse(self, edit: QLineEdit):
        path = edit.text().strip()

        if edit == self.target_edit and self.target_via_checkbox.isChecked():
            if not self._validate_via_output():
                return
            self._browse_target_via_output(path)
            return

        is_remote, user, host, real_path = parse_path(path)

        if is_remote:
            user_host = f"{user}@{host}"
            self.logger.info(f"Browse remote: {user_host}, current path: {real_path or '/'}")
            password = self.config.ssh_passwords.get(user_host)
            if not password:
                password = self._ask_password(user_host)
                if not password:
                    self.logger.info("Browse cancelled: no password")
                    return
            dialog = RemoteDirDialog(self.ssh_pool, user_host, password, real_path, self)
            if dialog.exec_() == QDialog.Accepted:
                selected = f"{user_host}:{dialog.selected_path}"
                edit.setText(selected)
                self.logger.info(f"Selected remote path: {selected}")
            else:
                self.logger.info("Browse cancelled by user")
        else:
            directory = QFileDialog.getExistingDirectory(self, "Select Directory", real_path or "")
            if directory:
                edit.setText(directory)
                self.logger.info(f"Selected local path: {directory}")

    def _browse_target_via_output(self, target_path: str):
        """通过 output 主机作为跳板浏览 target 目录。"""
        output = self.output_edit.text().strip()
        is_out_remote, out_user, out_host, out_real = parse_path(output)
        if not is_out_remote:
            QMessageBox.warning(self, "Error", "Output must be a remote path when using Via Output Host")
            return

        is_tgt_remote, tgt_user, tgt_host, tgt_real = parse_path(target_path)
        if not is_tgt_remote:
            QMessageBox.warning(self, "Error", "Target must be a remote path when using Via Output Host")
            return

        out_user_host = f"{out_user}@{out_host}"
        tgt_user_host = f"{tgt_user}@{tgt_host}"

        out_password = self.config.ssh_passwords.get(out_user_host)
        if not out_password:
            out_password = self._ask_password(out_user_host)
            if not out_password:
                return

        tgt_password = self.config.ssh_passwords.get(tgt_user_host)
        if not tgt_password:
            tgt_password = self._ask_password(tgt_user_host)
            if not tgt_password:
                return

        proxy_conn = self.ssh_pool.get(out_user_host, out_password)
        try:
            target_conn = proxy_conn.open_proxy_connection(tgt_host, tgt_user, tgt_password)
        except Exception as e:
            QMessageBox.warning(self, "Connection Failed", f"Failed to connect to target via output host: {e}")
            return

        dialog = RemoteDirDialog(
            self.ssh_pool, tgt_user_host, tgt_password,
            tgt_real or "/", self, proxy_conn=target_conn
        )
        if dialog.exec_() == QDialog.Accepted:
            selected = f"{tgt_user_host}:{dialog.selected_path}"
            self.target_edit.setText(selected)
            self.logger.info(f"Selected remote path: {selected}")

    def _ask_password(self, user_host: str) -> str:
        self.logger.info(f"Requesting password for {user_host}")
        dialog = PasswordDialog(user_host, self)
        if dialog.exec_() == QDialog.Accepted:
            password, remember = dialog.get_password()
            if remember:
                self.config.set_ssh_password(user_host, password)
                self.logger.info(f"Password remembered for {user_host}")
            try:
                conn = self.ssh_pool.get(user_host, password)
                if conn.verify_password(password):
                    self.logger.info(f"Password verified for {user_host}")
                    return password
            except Exception as e:
                self.logger.warning(f"Password verification failed for {user_host}: {e}")
                pass
            QMessageBox.warning(self, "Auth Failed", f"Failed to authenticate {user_host}")
            self.logger.warning(f"Authentication failed for {user_host}")
        else:
            self.logger.info(f"Password dialog cancelled for {user_host}")
        return ""

    def _validate_patch_input(self, require_backup: bool = False) -> bool:
        output = self.output_edit.text().strip()
        target = self.target_edit.text().strip()
        backup = self.backup_edit.text().strip()

        if not output or not target or (require_backup and not backup):
            msg = "Output, Target and Backup directories are required" if require_backup else "Output and Target directories are required"
            QMessageBox.warning(self, "Input Error", msg)
            return False

        is_remote, user, host, real_output = parse_path(output)
        if is_remote:
            user_host = f"{user}@{host}"
            password = self.config.ssh_passwords.get(user_host)
            if not password:
                password = self._ask_password(user_host)
                if not password:
                    return False
            try:
                conn = self.ssh_pool.get(user_host, password)
                try:
                    conn.sftp.stat(real_output)
                except Exception:
                    QMessageBox.warning(self, "Input Error", f"Output directory not found: {output}")
                    return False
                entries = conn.sftp.listdir(real_output)
                if not entries:
                    QMessageBox.warning(self, "Input Error", f"Output directory is empty: {output}")
                    return False
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to check output directory: {e}")
                return False
        else:
            if not os.path.exists(real_output):
                QMessageBox.warning(self, "Input Error", f"Output directory not found: {output}")
                return False
            if not os.path.isdir(real_output):
                QMessageBox.warning(self, "Input Error", f"Output path is not a directory: {output}")
                return False
            if not os.listdir(real_output):
                QMessageBox.warning(self, "Input Error", f"Output directory is empty: {output}")
                return False
        return True

    def _validate_via_output(self) -> bool:
        if not self.target_via_checkbox.isChecked():
            return True

        output = self.output_edit.text().strip()
        target = self.target_edit.text().strip()

        if not output:
            QMessageBox.warning(self, "Input Error", "Output directory is required when Via Output Host is checked")
            return False

        is_out_remote, _, _, _ = parse_path(output)
        if not is_out_remote:
            QMessageBox.warning(self, "Input Error", "Output must be a remote path (user@host:/path) when Via Output Host is checked")
            return False

        if not target:
            QMessageBox.warning(self, "Input Error", "Target directory is required")
            return False

        is_tgt_remote, _, _, _ = parse_path(target)
        if not is_tgt_remote:
            QMessageBox.warning(self, "Input Error", "Target must be a remote path (user@host:/path) when Via Output Host is checked")
            return False

        return True

    def _confirm_partial(self, source: str, target: str, overlapping: list) -> bool:
        if not overlapping:
            return True
        diff_text = self._build_partial_text(source, target, overlapping)
        dialog = ConfirmDialog("Partial Match", diff_text, self)
        if dialog.exec_() != QDialog.Accepted:
            self.logger.info("User rejected partial match confirmation")
            self._set_busy(False)
            return False
        self.logger.info("User confirmed partial match")
        return True

    def _confirm_overlapping(self, source: str, target: str, overlapping: list, title: str, action_desc: str, action_text: str) -> bool:
        if not overlapping:
            return True
        overlap_text = self._build_overwrite_text(source, target, overlapping, action_desc, action_text)
        dialog = ConfirmDialog(title, overlap_text, self)
        if dialog.exec_() != QDialog.Accepted:
            self.logger.info(f"User rejected {action_text} confirmation")
            self._set_busy(False)
            return False
        self.logger.info(f"User confirmed {action_text}")
        return True

    def _on_backup(self):
        if not self._validate_patch_input(require_backup=True):
            return
        if not self._validate_via_output():
            return
        self._set_busy(True)
        self._run_precheck(
            self.output_edit.text().strip(),
            self.target_edit.text().strip(),
            lambda status, overlapping: self._on_backup_precheck_done(
                status, overlapping,
                self.output_edit.text().strip(),
                self.target_edit.text().strip(),
                self.backup_edit.text().strip()
            )
        )

    def _on_backup_precheck_done(self, status: str, overlapping: list, output: str, target: str, backup: str):
        self.logger.info(f"Backup precheck done: status={status}, overlapping_count={len(overlapping)}")
        if overlapping:
            self.logger.info(f"Overlapping items: {overlapping}")

        if not overlapping:
            self.logger.info("No overlap detected, nothing to backup")
            QMessageBox.information(self, "Info", "No overlapping files to backup.")
            self._set_busy(False)
            return

        if not self._confirm_partial(target, backup, overlapping):
            return
        if not self._confirm_overlapping(target, backup, overlapping, "Confirm Backup", "Backup", "backed up"):
            return

        self._run_worker('backup_overlap', output, target, backup)

    def _on_patch(self):
        if not self._validate_patch_input():
            return
        if not self._validate_via_output():
            return
        self._set_busy(True)
        self._run_precheck(
            self.output_edit.text().strip(),
            self.target_edit.text().strip(),
            lambda status, overlapping: self._on_precheck_done(
                status, overlapping, 'patch',
                self.output_edit.text().strip(),
                self.target_edit.text().strip(),
                self.backup_edit.text().strip()
            )
        )

    def _on_rollback(self):
        backup = self.backup_edit.text().strip()
        target = self.target_edit.text().strip()

        self.logger.info(f"Rollback clicked: backup={backup}, target={target}")

        if not backup or not target:
            self.logger.warning("Rollback aborted: Backup or Target empty")
            QMessageBox.warning(self, "Input Error", "Backup and Target directories are required")
            return

        backup_path = self.rollback_combo.currentData()
        if not backup_path:
            self.logger.warning("Rollback aborted: no backup selected")
            QMessageBox.warning(self, "Input Error", "Please select a backup version")
            return

        self.logger.info(f"Rollback backup selected: {backup_path}")
        if not self._validate_via_output():
            return
        self._set_busy(True)
        output = self.output_edit.text().strip()
        self._run_precheck(backup_path, target, lambda status, overlapping: self._on_precheck_done(
            status, overlapping, 'rollback', backup_path, target, backup
        ), proxy_output_path=output)

    def _run_precheck(self, source_path: str, target_path: str, callback, proxy_output_path=None):
        self.logger.info(f"Start precheck: {source_path} -> {target_path}")
        self.current_thread = PreCheckThread(
            source_path, target_path, self.ssh_pool, self.config,
            target_via_output=self.target_via_checkbox.isChecked(),
            proxy_output_path=proxy_output_path
        )
        self.current_thread.result.connect(lambda status, overlapping: callback(status, overlapping))
        self.current_thread.error.connect(self._on_thread_error)
        self.current_thread.log.connect(self._on_log)
        self.current_thread.finished.connect(self._on_thread_finished)
        self.current_thread.start()

    def _on_precheck_done(self, status: str, overlapping: list, op_type: str, source: str, target: str, backup: str):
        # 立即断开 PreCheckThread 的 finished 信号，避免在确认对话框 exec_() 期间误触发兜底恢复
        if self.current_thread is not None:
            try:
                self.current_thread.finished.disconnect(self._on_thread_finished)
            except TypeError:
                pass

        self.logger.info(f"Precheck done: status={status}, overlapping_count={len(overlapping)}")
        if overlapping:
            self.logger.info(f"Overlapping items: {overlapping}")

        if status in (CompatStatus.NONE.value, CompatStatus.EMPTY_TARGET.value):
            self.logger.warning("No overlap detected, showing confirmation dialog")
            none_text = self._build_no_overlap_text(source, target)
            dialog = ConfirmDialog("No Overlap", none_text, self)
            if dialog.exec_() != QDialog.Accepted:
                self.logger.info("User rejected no-overlap confirmation")
                self._set_busy(False)
                return
            self.logger.info("User confirmed no-overlap continuation")

        if not self._confirm_partial(source, target, overlapping):
            return
        if op_type == 'rollback':
            if not self._confirm_overlapping(source, target, overlapping, "Confirm Rollback", "Rollback", "overwritten"):
                return
        else:
            if not self._confirm_overlapping(source, target, overlapping, "Confirm Overwrite", "Overwrite", "overwritten"):
                return

        self._run_worker(op_type, source, target, backup)

    def _truncate_path(self, path: str, max_len: int = 60) -> str:
        if len(path) <= max_len:
            return path
        prefix_len = max_len // 2 - 2
        suffix_len = max_len // 2 - 1
        return path[:prefix_len] + "..." + path[-suffix_len:]

    def _build_partial_text(self, source: str, target: str, overlapping: list) -> str:
        text = "## Partial Match Detected\n\n"
        text += f"**From:** `{self._truncate_path(source)}`\n"
        text += f"**To:** `{self._truncate_path(target)}`\n\n"
        text += "The following items exist in both directories:\n\n"
        text += "| Type | Item |\n|------|------|\n"
        for item in overlapping[:20]:
            type_label = "Dir" if item.get("is_dir") else "File"
            text += f"| {type_label} | {item['name']} |\n"
        if len(overlapping) > 20:
            text += f"| ... | and {len(overlapping) - 20} more |\n"
        if any(item.get("is_dir") for item in overlapping):
            text += "\n**Warning:** Directories will be completely removed and replaced.\n"
        if self.target_via_checkbox.isChecked():
            text += "\n**Note:** Target is accessed via output host (jump host).\n"
        text += "\nDo you want to continue?"
        return text

    def _build_overwrite_text(self, source: str, target: str, overlapping: list, action_desc: str, action_text: str) -> str:
        text = f"## Confirm {action_desc}\n\n"
        text += f"**From:** `{self._truncate_path(source)}`\n"
        text += f"**To:** `{self._truncate_path(target)}`\n\n"
        text += f"The following items will be {action_text}:\n\n"
        text += "| Type | Item |\n|------|------|\n"
        for item in overlapping[:20]:
            type_label = "Dir" if item.get("is_dir") else "File"
            text += f"| {type_label} | {item['name']} |\n"
        if len(overlapping) > 20:
            text += f"| ... | and {len(overlapping) - 20} more |\n"
        if any(item.get("is_dir") for item in overlapping):
            text += "\n**Warning:** Directories will be completely removed and replaced.\n"
        if self.target_via_checkbox.isChecked():
            text += "\n**Note:** Target is accessed via output host (jump host).\n"
        text += "\nContinue?"
        return text

    def _build_no_overlap_text(self, source: str, target: str) -> str:
        text = "## Warning: No Overlap Detected\n\n"
        text += f"**From:** `{self._truncate_path(source)}`\n"
        text += f"**To:** `{self._truncate_path(target)}`\n\n"
        text += "Source and target have no common items. "
        text += "This means the target will receive all new content.\n\n"
        if self.target_via_checkbox.isChecked():
            text += "**Note:** Target is accessed via output host (jump host).\n\n"
        text += "Do you want to continue?"
        return text

    def _run_worker(self, op_type: str, source: str, target: str, backup: str):
        self.logger.info(f"Start worker: type={op_type}, source={source}, target={target}")
        paths = {'output': source, 'target': target, 'backup': backup}
        if op_type == 'rollback':
            paths = {'backup': source, 'target': target, 'backup_dir': backup, 'output': self.output_edit.text().strip()}

        self.current_thread = WorkerThread(
            op_type, paths, self.ssh_pool, self.config,
            target_via_output=self.target_via_checkbox.isChecked()
        )
        self.current_thread.progress.connect(self._on_progress)
        self.current_thread.log.connect(self._on_log)
        self.current_thread.finished_sig.connect(self._on_worker_finished)
        self.current_thread.error.connect(self._on_thread_error)
        self.current_thread.start()

    def _on_worker_finished(self, success: bool, message: str):
        self.logger.info(f"Worker finished: success={success}, message={message}")
        self._set_busy(False)
        if success:
            QMessageBox.information(self, "Success", message)
        else:
            QMessageBox.critical(self, "Failed", message)

    def _on_thread_error(self, msg: str):
        self.logger.error(f"Thread error: {msg}")
        self._set_busy(False)
        if msg.startswith("AUTH:"):
            user_host = msg.split(":", 1)[1].strip()
            self.logger.info(f"Auth required for {user_host}")
            password = self._ask_password(user_host)
            if password:
                self.logger.info(f"Retrying with new password for {user_host}")
        else:
            QMessageBox.critical(self, "Error", msg)

    def _on_thread_finished(self):
        # PreCheckThread 的 finished 信号不需要兜底恢复，因为 result/error 槽已经处理了 UI 状态
        sender = self.sender()
        if isinstance(sender, PreCheckThread):
            return
        # 兜底：如果线程结束但 busy 仍为 True，强制恢复
        if not self.patch_btn.isEnabled():
            self.logger.warning("Thread finished but busy state not reset, forcing restore")
            self._set_busy(False)

    def _on_progress(self, step: str, detail: str):
        self._on_log(f"[{step}] {detail}")

    def _on_log(self, msg: str):
        self.logger.info(msg)
        self.log_edit.append(msg)
        scrollbar = self.log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_busy(self, busy: bool):
        self.patch_btn.setEnabled(not busy)
        self.rollback_btn.setEnabled(not busy)
        self.backup_btn.setEnabled(not busy)
        self.output_btn.setEnabled(not busy)
        self.target_btn.setEnabled(not busy)
        self.save_config_btn.setEnabled(not busy)
        self.load_config_btn.setEnabled(not busy)
        if busy:
            QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        else:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def _refresh_backups(self):
        backup_dir = self.backup_edit.text().strip()
        self.logger.info(f"Refresh backups: {backup_dir}")
        if not backup_dir:
            self.logger.warning("Refresh aborted: backup dir empty")
            self.rollback_combo.clear()
            return
        self.rollback_combo.clear()
        self.rollback_combo.addItem("Loading...")

        self._list_thread = ListBackupsThread(backup_dir, self.ssh_pool, self.config)
        self._list_thread.result.connect(self._on_backups_loaded)
        self._list_thread.error.connect(self._on_backups_error)
        self._list_thread.start()

    def _on_backups_loaded(self, backups: list):
        self.rollback_combo.clear()
        for name, full_path in backups:
            self.rollback_combo.addItem(name, full_path)
        if backups:
            self.logger.info(f"Found {len(backups)} backups")
        else:
            self.logger.info("No backups found")

    def _on_backups_error(self, msg: str):
        self.rollback_combo.clear()
        if msg.startswith("AUTH:"):
            user_host = msg.split(":", 1)[1].strip()
            password = self._ask_password(user_host)
            if password:
                self._refresh_backups()
            return
        self.logger.error(f"Failed to list backups: {msg}")
        QMessageBox.warning(self, "Error", f"Failed to list backups: {msg}")

    def _save_config(self):
        self.config.set("backup", self.backup_edit.text().strip())
        self.config.set("output", self.output_edit.text().strip())
        self.config.set("target", self.target_edit.text().strip())
        self.config.set("target_via_output", self.target_via_checkbox.isChecked())
        self.config.save(self.config_path)
        self.logger.info(f"Config saved to {self.config_path}")

    def _load_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Config", "conf", "JSON (*.json)")
        if path:
            self.config.load(path)
            self.config_path = path
            self._load_defaults()
            self.logger.info(f"Config loaded from {path}")

    def closeEvent(self, event):
        self.logger.info("Application closing")
        self.ssh_pool.clear_all()
        event.accept()
