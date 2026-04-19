"""
Patch / Rollback / Backup 操作类
每个操作类接收两个 FileSystem 实例（源、目标），run() 执行并返回结果
"""

import logging
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

from lib.fs import FileSystem, LocalFS, RemoteFS, TempLocalFS


@dataclass
class OperationResult:
    success: bool
    message: str


class BaseOperation(ABC):
    def __init__(
        self,
        source_fs: FileSystem,
        target_fs: FileSystem,
        logger: logging.Logger,
        progress_callback: Optional[Callable[[str, str], None]] = None,
        cancelled_callback: Optional[Callable[[], bool]] = None,
    ):
        self.source_fs = source_fs
        self.target_fs = target_fs
        self.logger = logger
        self.progress_callback = progress_callback
        self.cancelled_callback = cancelled_callback

    def _report(self, step: str, detail: str = "") -> None:
        self.logger.info(f"[{step}] {detail}")
        if self.progress_callback:
            self.progress_callback(step, detail)

    def _is_cancelled(self) -> bool:
        if self.cancelled_callback:
            return self.cancelled_callback()
        return False

    @abstractmethod
    def run(self) -> OperationResult:
        pass


class PatchOperation(BaseOperation):
    """将 source 的内容复制到 target，覆盖已有文件。"""

    def __init__(
        self,
        source_fs: FileSystem,
        target_fs: FileSystem,
        source_path: str,
        target_path: str,
        logger: logging.Logger,
        progress_callback: Optional[Callable[[str, str], None]] = None,
        cancelled_callback: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(source_fs, target_fs, logger, progress_callback, cancelled_callback)
        self.source_path = source_path
        self.target_path = target_path

    def run(self) -> OperationResult:
        if not self.source_fs.exists(self.source_path):
            return OperationResult(False, f"Source not found: {self.source_path}")

        self._report("patch", f"Copying {self.source_path} -> {self.target_path}")

        try:
            self._copy_recursive(self.source_path, self.target_path)
        except Exception as e:
            self.logger.exception("Patch failed")
            return OperationResult(False, f"Patch failed: {e}")

        self._report("patch", "Done")
        return OperationResult(True, "Patch completed successfully")

    def _copy_recursive(self, src: str, dst: str) -> None:
        if self._is_cancelled():
            raise RuntimeError("Operation cancelled")

        if self.source_fs.isfile(src):
            self._copy_file(src, dst)
        elif self.source_fs.isdir(src):
            self.target_fs.makedirs(dst, exist_ok=True)
            for name in self.source_fs.listdir(src):
                self._copy_recursive(
                    self.source_fs.join(src, name),
                    self.target_fs.join(dst, name),
                )

    def _copy_file(self, src: str, dst: str) -> None:
        # Local -> Local
        if isinstance(self.source_fs, LocalFS) and isinstance(self.target_fs, LocalFS):
            self.target_fs.copy(src, dst)
            return

        # Local -> Remote
        if isinstance(self.source_fs, LocalFS) and isinstance(self.target_fs, RemoteFS):
            self.target_fs.upload_file(src, dst)
            return

        # Remote -> Local
        if isinstance(self.source_fs, RemoteFS) and isinstance(self.target_fs, LocalFS):
            self.source_fs.download_file(src, dst)
            return

        # Remote -> Remote (SFTP 流式中转，不落地本地磁盘)
        if isinstance(self.source_fs, RemoteFS) and isinstance(self.target_fs, RemoteFS):
            src_file = self.source_fs.sftp.open(src, 'rb')
            try:
                self.target_fs.sftp.putfo(src_file, dst)
            finally:
                src_file.close()
            return

        raise RuntimeError("Unsupported file system combination")


class RollbackOperation(BaseOperation):
    """将 backup 恢复到 target。"""

    def __init__(
        self,
        backup_fs: FileSystem,
        target_fs: FileSystem,
        backup_path: str,
        target_path: str,
        logger: logging.Logger,
        progress_callback: Optional[Callable[[str, str], None]] = None,
        cancelled_callback: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(backup_fs, target_fs, logger, progress_callback, cancelled_callback)
        self.backup_path = backup_path
        self.target_path = target_path

    def run(self) -> OperationResult:
        if not self.source_fs.exists(self.backup_path):
            return OperationResult(False, f"Backup not found: {self.backup_path}")

        self._report("rollback", f"Restoring {self.backup_path} -> {self.target_path}")

        try:
            op = PatchOperation(
                self.source_fs, self.target_fs,
                self.backup_path, self.target_path,
                self.logger, self.progress_callback, self.cancelled_callback,
            )
            result = op.run()
            if result.success:
                return OperationResult(True, "Rollback completed successfully")
            return result
        except Exception as e:
            self.logger.exception("Rollback failed")
            return OperationResult(False, f"Rollback failed: {e}")


class BackupOperation(BaseOperation):
    """将 target 完整备份到 backup_dir，命名格式 {basename}_YYYYMMDD_HHMMSS。"""

    def __init__(
        self,
        target_fs: FileSystem,
        backup_fs: FileSystem,
        target_path: str,
        backup_dir: str,
        logger: logging.Logger,
        progress_callback: Optional[Callable[[str, str], None]] = None,
        cancelled_callback: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(target_fs, backup_fs, logger, progress_callback, cancelled_callback)
        self.target_path = target_path
        self.backup_dir = backup_dir

    def run(self) -> OperationResult:
        if not self.source_fs.exists(self.target_path):
            return OperationResult(False, f"Target not found: {self.target_path}")

        from datetime import datetime
        basename = self.source_fs.basename(self.target_path) or "target"
        backup_name = f"{basename}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        backup_path = self.target_fs.join(self.backup_dir, backup_name)

        self._report("backup", f"Backing up {self.target_path} -> {backup_path}")

        try:
            op = PatchOperation(
                self.source_fs, self.target_fs,
                self.target_path, backup_path,
                self.logger, self.progress_callback, self.cancelled_callback,
            )
            result = op.run()
            if result.success:
                return OperationResult(True, f"Backup completed: {backup_name}")
            return result
        except Exception as e:
            self.logger.exception("Backup failed")
            return OperationResult(False, f"Backup failed: {e}")
