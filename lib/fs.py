"""
文件系统抽象层
统一本地/远程操作接口，上层无感知
"""

import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from lib.ssh_client import SSHConnection, SSHPool, SFTP_TIMEOUT


class FileSystem(ABC):
    @abstractmethod
    def listdir(self, path: str) -> list[str]:
        """列出目录内容，返回相对名列表。"""

    @abstractmethod
    def exists(self, path: str) -> bool:
        pass

    @abstractmethod
    def isdir(self, path: str) -> bool:
        pass

    @abstractmethod
    def isfile(self, path: str) -> bool:
        pass

    @abstractmethod
    def mkdir(self, path: str, exist_ok: bool = False) -> None:
        pass

    @abstractmethod
    def copy(self, src: str, dst: str) -> None:
        """文件或目录递归复制。"""

    @abstractmethod
    def remove(self, path: str) -> None:
        """文件或目录递归删除。"""

    @abstractmethod
    def join(self, *parts: str) -> str:
        pass

    @abstractmethod
    def basename(self, path: str) -> str:
        pass

    @abstractmethod
    def dirname(self, path: str) -> str:
        pass

    @abstractmethod
    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        pass


class LocalFS(FileSystem):
    """基于 pathlib / shutil 的本地文件系统实现。"""

    @staticmethod
    def _resolve(path: str) -> str:
        return os.path.expanduser(path)

    def listdir(self, path: str) -> list[str]:
        path = self._resolve(path)
        if not os.path.isdir(path):
            return []
        return [name for name in os.listdir(path) if not name.startswith('.')]

    def exists(self, path: str) -> bool:
        return os.path.exists(self._resolve(path))

    def isdir(self, path: str) -> bool:
        return os.path.isdir(self._resolve(path))

    def isfile(self, path: str) -> bool:
        return os.path.isfile(self._resolve(path))

    def mkdir(self, path: str, exist_ok: bool = False) -> None:
        os.makedirs(self._resolve(path), exist_ok=exist_ok)

    def copy(self, src: str, dst: str) -> None:
        src = self._resolve(src)
        dst = self._resolve(dst)
        if os.path.isfile(src):
            shutil.copy2(src, dst)
        elif os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

    def remove(self, path: str) -> None:
        path = self._resolve(path)
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)

    def join(self, *parts: str) -> str:
        return os.path.join(*parts)

    def basename(self, path: str) -> str:
        return os.path.basename(path)

    def dirname(self, path: str) -> str:
        return os.path.dirname(path)

    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        os.makedirs(self._resolve(path), exist_ok=exist_ok)


class RemoteFS(FileSystem):
    """
    基于 paramiko.SFTPClient 的远程文件系统实现。
    路径按 Linux 规则处理。
    """

    def __init__(self, ssh_conn: SSHConnection):
        self.ssh_conn = ssh_conn
        self.sftp = ssh_conn.sftp

    def listdir(self, path: str) -> list[str]:
        if self.sftp is None:
            return []
        try:
            entries = self.sftp.listdir(path)
            return [name for name in entries if not name.startswith('.')]
        except Exception:
            return []

    def exists(self, path: str) -> bool:
        if self.sftp is None:
            return False
        try:
            self.sftp.stat(path)
            return True
        except Exception:
            return False

    def isdir(self, path: str) -> bool:
        if self.sftp is None:
            return False
        try:
            st = self.sftp.stat(path)
            return (st.st_mode & 0o170000) == 0o040000
        except Exception:
            return False

    def isfile(self, path: str) -> bool:
        if self.sftp is None:
            return False
        try:
            st = self.sftp.stat(path)
            return (st.st_mode & 0o170000) == 0o100000
        except Exception:
            return False

    def mkdir(self, path: str, exist_ok: bool = False) -> None:
        if self.sftp is None:
            raise RuntimeError("SFTP not connected")
        try:
            self.sftp.mkdir(path)
        except Exception:
            if not exist_ok:
                raise

    def copy(self, src: str, dst: str) -> None:
        """远程到远程复制：先下载到本地 temp，再上传。"""
        raise NotImplementedError("RemoteFS.copy 请通过中转实现")

    def remove(self, path: str) -> None:
        if self.sftp is None:
            raise RuntimeError("SFTP not connected")
        self._rm_recursive(path)

    def _rm_recursive(self, path: str) -> None:
        try:
            st = self.sftp.stat(path)
            is_dir = (st.st_mode & 0o170000) == 0o040000
        except Exception:
            return
        if is_dir:
            for name in self.sftp.listdir(path):
                self._rm_recursive(f"{path}/{name}")
            self.sftp.rmdir(path)
        else:
            self.sftp.remove(path)

    def join(self, *parts: str) -> str:
        return '/'.join(parts)

    def basename(self, path: str) -> str:
        return path.rsplit('/', 1)[-1]

    def dirname(self, path: str) -> str:
        if '/' not in path:
            return '.'
        return path.rsplit('/', 1)[0] or '/'

    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        if self.sftp is None:
            raise RuntimeError("SFTP not connected")
        if path == '/' or path == '':
            return
        if self.exists(path):
            if not exist_ok:
                raise FileExistsError(path)
            return
        parent = self.dirname(path)
        if parent and parent != path:
            self.makedirs(parent, exist_ok=True)
        try:
            self.sftp.mkdir(path)
        except Exception:
            if not exist_ok:
                raise

    def download_file(self, remote_path: str, local_path: str) -> None:
        """从远程下载文件到本地。"""
        if self.sftp is None:
            raise RuntimeError("SFTP not connected")
        local_path = os.path.expanduser(local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        self.sftp.get(remote_path, local_path)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        """从本地上传文件到远程。"""
        if self.sftp is None:
            raise RuntimeError("SFTP not connected")
        local_path = os.path.expanduser(local_path)
        self.makedirs(self.dirname(remote_path), exist_ok=True)
        self.sftp.put(local_path, remote_path)

    def download_dir(self, remote_path: str, local_path: str) -> None:
        """从远程下载目录到本地。"""
        if self.sftp is None:
            raise RuntimeError("SFTP not connected")
        local_path = os.path.expanduser(local_path)
        os.makedirs(local_path, exist_ok=True)
        for name in self.sftp.listdir(remote_path):
            rpath = f"{remote_path}/{name}"
            lpath = os.path.join(local_path, name)
            if self.isdir(rpath):
                self.download_dir(rpath, lpath)
            else:
                self.sftp.get(rpath, lpath)

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        """从本地上传目录到远程。"""
        if self.sftp is None:
            raise RuntimeError("SFTP not connected")
        local_path = os.path.expanduser(local_path)
        self.makedirs(remote_path, exist_ok=True)
        for name in os.listdir(local_path):
            lpath = os.path.join(local_path, name)
            rpath = f"{remote_path}/{name}"
            if os.path.isdir(lpath):
                self.upload_dir(lpath, rpath)
            else:
                self.sftp.put(lpath, rpath)


class TempLocalFS(LocalFS):
    """用于远程到远程中转的临时本地文件系统。"""

    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix='backup_tool_')

    def cleanup(self) -> None:
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def __del__(self):
        self.cleanup()


def parse_path(path: str) -> tuple[bool, Optional[str], Optional[str], str]:
    """
    解析路径，返回 (is_remote, user, host, real_path)。
    远程路径格式: user@host:/path
    """
    if path.startswith('~'):
        path = os.path.expanduser(path)

    if '@' in path and ':' in path:
        # 远程路径
        at_idx = path.index('@')
        colon_idx = path.index(':', at_idx)
        user = path[:at_idx]
        host = path[at_idx + 1:colon_idx]
        real_path = path[colon_idx + 1:]
        return True, user, host, real_path

    return False, None, None, os.path.abspath(path)
