"""
后台工作线程
- PreCheckThread: 执行兼容性检查和重叠检测
- WorkerThread: 执行实际的补丁/回滚/备份操作
"""

from typing import Optional

from gui.qt_compat import QThread, Signal

from lib.compat import check_patch_compatibility, find_overlapping_paths, CompatStatus
from lib.fs import parse_path, LocalFS, RemoteFS
from lib.ssh_client import SSHPool, AuthenticationError
from lib.operations import PatchOperation, RollbackOperation, BackupOperation
from lib.logger import AppLogger


class ListBackupsThread(QThread):
    """异步获取 backup 目录下的备份列表。"""
    result = Signal(list)   # [(name, full_path), ...]
    error = Signal(str)

    def __init__(self, backup_dir: str, ssh_pool: SSHPool, config):
        super().__init__()
        self.backup_dir = backup_dir
        self.ssh_pool = ssh_pool
        self.config = config
        self.logger = AppLogger.setup()

    def run(self):
        try:
            is_remote, user, host, real_path = parse_path(self.backup_dir)
            prefix = ""
            if is_remote:
                user_host = f"{user}@{host}"
                password = self.config.ssh_passwords.get(user_host)
                if not password:
                    self.error.emit(f"AUTH:{user_host}")
                    return
                conn = self.ssh_pool.get(user_host, password)
                fs = RemoteFS(conn)
                prefix = f"{user_host}:"
            else:
                fs = LocalFS()

            entries = fs.listdir(real_path)
            import re
            backups = []
            for name in entries:
                if re.match(r'.*_\d{8}_\d{6}$', name):
                    full_path = prefix + fs.join(real_path, name)
                    backups.append((name, full_path))
            backups.sort(key=lambda x: x[0], reverse=True)
            self.result.emit(backups)
        except AuthenticationError as e:
            self.error.emit(f"AUTH:{e}")
        except Exception as e:
            self.logger.exception("List backups failed")
            self.error.emit(str(e))


class PreCheckThread(QThread):
    """预检线程：兼容性检查 + 重叠检测。"""
    result = Signal(str, list)   # status, overlapping_paths
    error = Signal(str)
    log = Signal(str)

    def __init__(self, output_path: str, target_path: str, ssh_pool: SSHPool, config):
        super().__init__()
        self.output_path = output_path
        self.target_path = target_path
        self.ssh_pool = ssh_pool
        self.config = config
        self.logger = AppLogger.setup()

    def run(self):
        try:
            output_fs, output_real = self._get_fs(self.output_path)
            target_fs, target_real = self._get_fs(self.target_path)

            self.logger.info("PreCheck: checking compatibility...")
            self.log.emit("Checking compatibility...")
            status = check_patch_compatibility(output_fs, target_fs, output_real, target_real)
            self.logger.info(f"PreCheck: compatibility status = {status.value}")
            self.log.emit(f"Compatibility status: {status.value}")

            overlapping = find_overlapping_paths(output_fs, target_fs, output_real, target_real)
            self.logger.info(f"PreCheck: overlapping count = {len(overlapping)}")
            self.result.emit(status.value, overlapping)
        except AuthenticationError as e:
            self.logger.error(f"PreCheck auth error: {e}")
            self.error.emit(f"AUTH:{e}")
        except Exception as e:
            self.logger.exception("PreCheck failed")
            self.error.emit(str(e))

    def _get_fs(self, path: str):
        is_remote, user, host, real_path = parse_path(path)
        if is_remote:
            user_host = f"{user}@{host}"
            password = self.config.ssh_passwords.get(user_host)
            conn = self.ssh_pool.get(user_host, password)
            return RemoteFS(conn), real_path
        return LocalFS(), real_path


class WorkerThread(QThread):
    """工作线程：执行备份 + 操作。"""
    progress = Signal(str, str)   # step, detail
    log = Signal(str)
    finished_sig = Signal(bool, str)  # success, message
    error = Signal(str)

    def __init__(
        self,
        operation_type: str,   # 'patch' | 'rollback' | 'backup'
        paths: dict,
        ssh_pool: SSHPool,
        config,
    ):
        super().__init__()
        self.operation_type = operation_type
        self.paths = paths
        self.ssh_pool = ssh_pool
        self.config = config
        self.logger = AppLogger.setup()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        try:
            if self.operation_type == 'patch':
                self._do_patch()
            elif self.operation_type == 'rollback':
                self._do_rollback()
            elif self.operation_type == 'backup':
                self._do_backup()
            elif self.operation_type == 'backup_overlap':
                self._do_backup_overlap()
            else:
                self.error.emit(f"Unknown operation: {self.operation_type}")
        except AuthenticationError as e:
            self.error.emit(f"AUTH:{e}")
        except Exception as e:
            self.logger.exception("Worker failed")
            self.error.emit(str(e))

    def _backup_overlapping(self, require_backup: bool = False) -> Optional[str]:
        """备份重叠文件，返回备份名；require_backup=True 时无备份目录视为错误。"""
        from lib.compat import backup_overlapping_files

        output_path = self.paths['output']
        target_path = self.paths['target']
        backup_dir = self.paths.get('backup', '')

        output_fs, output_real = self._get_fs(output_path)
        target_fs, target_real = self._get_fs(target_path)
        backup_fs, backup_real = self._get_fs(backup_dir) if backup_dir else (LocalFS(), backup_dir)

        if require_backup and not backup_dir:
            self.logger.warning("Backup overlap: no backup dir specified")
            self.finished_sig.emit(False, "Backup directory is required")
            return None

        if not backup_dir:
            return None

        self.logger.info("Backing up overlapping files...")
        self.log.emit("Backing up overlapping files...")
        backup_name = backup_overlapping_files(
            output_fs, target_fs, output_real, target_real,
            backup_fs, backup_real, self.logger,
            self._is_cancelled,
        )
        if backup_name:
            self.logger.info(f"Backup saved as {backup_name}")
            self.log.emit(f"Backup saved: {backup_name}")
        else:
            self.logger.info("No overlapping files to backup")
        return backup_name

    def _do_patch(self):
        self.logger.info(f"Patch: output={self.paths['output']}, target={self.paths['target']}")

        self._backup_overlapping()

        if self._is_cancelled():
            self.logger.info("Patch: cancelled by user")
            self.finished_sig.emit(False, "Cancelled by user")
            return

        output_fs, output_real = self._get_fs(self.paths['output'])
        target_fs, target_real = self._get_fs(self.paths['target'])

        self.logger.info("Patch: applying patch...")
        self.log.emit("Applying patch...")
        op = PatchOperation(
            output_fs, target_fs, output_real, target_real,
            self.logger, self._on_progress, self._is_cancelled,
        )
        result = op.run()
        self.logger.info(f"Patch: result success={result.success}, message={result.message}")
        self.finished_sig.emit(result.success, result.message)

    def _do_backup_overlap(self):
        self.logger.info(f"BackupOverlap: output={self.paths['output']}, target={self.paths['target']}")

        backup_name = self._backup_overlapping(require_backup=True)
        if backup_name is None:
            self.finished_sig.emit(True, "No overlapping files to backup")
            return

        if self._is_cancelled():
            self.logger.info("BackupOverlap: cancelled by user")
            self.finished_sig.emit(False, "Cancelled by user")
            return

        if backup_name:
            self.finished_sig.emit(True, f"Backup completed: {backup_name}")
        else:
            self.finished_sig.emit(True, "No overlapping files to backup")

    def _do_rollback(self):
        backup_path = self.paths['backup']
        target_path = self.paths['target']

        backup_fs, backup_real = self._get_fs(backup_path)
        target_fs, target_real = self._get_fs(target_path)

        self.logger.info(f"Rollback: backup={backup_real}, target={target_real}")

        if self._is_cancelled():
            self.logger.info("Rollback: cancelled by user")
            self.finished_sig.emit(False, "Cancelled by user")
            return

        self.logger.info("Rollback: starting rollback...")
        self.log.emit("Rolling back...")
        op = RollbackOperation(
            backup_fs, target_fs, backup_real, target_real,
            self.logger, self._on_progress, self._is_cancelled,
        )
        result = op.run()
        self.logger.info(f"Rollback: result success={result.success}, message={result.message}")
        self.finished_sig.emit(result.success, result.message)

    def _do_backup(self):
        target_path = self.paths['target']
        backup_dir = self.paths['backup']

        target_fs, target_real = self._get_fs(target_path)
        backup_fs, backup_real = self._get_fs(backup_dir)

        self.logger.info(f"Backup: target={target_real}, backup_dir={backup_real}")

        if self._is_cancelled():
            self.logger.info("Backup: cancelled by user")
            self.finished_sig.emit(False, "Cancelled by user")
            return

        self.logger.info("Backup: creating backup...")
        self.log.emit("Creating backup...")
        op = BackupOperation(
            target_fs, backup_fs, target_real, backup_real,
            self.logger, self._on_progress, self._is_cancelled,
        )
        result = op.run()
        self.logger.info(f"Backup: result success={result.success}, message={result.message}")
        self.finished_sig.emit(result.success, result.message)

    def _get_fs(self, path: str):
        from lib.fs import parse_path, LocalFS, RemoteFS
        is_remote, user, host, real_path = parse_path(path)
        if is_remote:
            user_host = f"{user}@{host}"
            password = self.config.ssh_passwords.get(user_host)
            conn = self.ssh_pool.get(user_host, password)
            return RemoteFS(conn), real_path
        return LocalFS(), real_path

    def _on_progress(self, step: str, detail: str):
        self.progress.emit(step, detail)
        self.logger.info(f"[{step}] {detail}")
        self.log.emit(f"[{step}] {detail}")
