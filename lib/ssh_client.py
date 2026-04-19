"""
SSH 连接池与密码管理
- 按 user@host 缓存 SSHConnection
- 所有连接和操作均带超时控制
- 密码未命中时抛出 AuthenticationError，由 GUI 捕获并弹窗
"""

import time
from typing import Optional

import paramiko

CONNECTION_TIMEOUT = 10
AUTH_TIMEOUT = 10
COMMAND_TIMEOUT = 30
SFTP_TIMEOUT = 60
TRANSFER_CHUNK_SIZE = 32768


class AuthenticationError(Exception):
    pass


class ConnectionTimeoutError(Exception):
    pass


class SFTPTimeoutError(Exception):
    pass


class SSHConnection:
    """封装 paramiko.SSHClient + SFTPClient，支持复用。"""

    def __init__(self, host: str, user: str, password: str, sock=None):
        self.host = host
        self.user = user
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.sftp: Optional[paramiko.SFTPClient] = None
        self._connect(password, sock)

    def _connect(self, password: str, sock=None) -> None:
        try:
            self.client.connect(
                hostname=self.host,
                username=self.user,
                password=password,
                timeout=CONNECTION_TIMEOUT,
                banner_timeout=CONNECTION_TIMEOUT,
                auth_timeout=AUTH_TIMEOUT,
                look_for_keys=False,
                sock=sock,
            )
            self.sftp = self.client.open_sftp()
            if self.sftp:
                channel = self.sftp.get_channel()
                if channel:
                    channel.settimeout(SFTP_TIMEOUT)
        except paramiko.AuthenticationException as e:
            raise AuthenticationError(f"Authentication failed for {self.user}@{self.host}") from e
        except TimeoutError as e:
            raise ConnectionTimeoutError(f"Connection timeout to {self.host}") from e
        except Exception as e:
            raise ConnectionTimeoutError(f"Failed to connect {self.host}: {e}") from e

    def verify_password(self, password: str) -> bool:
        """验证密码是否正确，验证后关闭连接。"""
        test_client = paramiko.SSHClient()
        test_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            test_client.connect(
                hostname=self.host,
                username=self.user,
                password=password,
                timeout=CONNECTION_TIMEOUT,
                banner_timeout=CONNECTION_TIMEOUT,
                auth_timeout=AUTH_TIMEOUT,
                look_for_keys=False,
            )
            test_client.close()
            return True
        except paramiko.AuthenticationException:
            return False
        except TimeoutError:
            return False
        except Exception:
            return False

    def exec_command(self, command: str) -> tuple:
        """执行远程命令，带超时控制。"""
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            raise ConnectionTimeoutError("SSH connection lost")
        channel = transport.open_session()
        channel.settimeout(COMMAND_TIMEOUT)
        channel.exec_command(command)
        stdout = channel.makefile('r', -1)
        stderr = channel.makefile_stderr('r', -1)
        exit_status = channel.recv_exit_status()
        return exit_status, stdout.read(), stderr.read()

    def open_proxy_connection(self, target_host: str, target_user: str, target_password: str) -> "SSHConnection":
        """
        通过当前 SSH 连接建立到 target 的跳板连接。
        使用 SSH direct-tcpip 通道转发，gateway 上无需执行任何远程命令。
        """
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            raise ConnectionTimeoutError("SSH connection lost")

        channel = transport.open_channel(
            "direct-tcpip",
            (target_host, 22),
            ("127.0.0.1", 0),
        )
        if channel is None:
            raise ConnectionTimeoutError(f"Failed to open proxy channel to {target_host}:22")

        return SSHConnection(target_host, target_user, target_password, sock=channel)

    def close(self) -> None:
        if self.sftp:
            try:
                self.sftp.close()
            except Exception:
                pass
            self.sftp = None
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None


class SSHPool:
    """连接池：按 user@host 缓存 SSHConnection。"""

    def __init__(self):
        self._pool: dict[str, SSHConnection] = {}

    def get(self, user_host: str, password: Optional[str] = None) -> SSHConnection:
        """获取或创建 SSH 连接。"""
        if user_host in self._pool:
            conn = self._pool[user_host]
            transport = conn.client.get_transport()
            if transport and transport.is_active():
                return conn
            # 连接已失效，移除
            self.clear(user_host)

        if password is None:
            raise AuthenticationError(f"No password provided for {user_host}")

        parts = user_host.split('@')
        if len(parts) != 2:
            raise ValueError(f"Invalid user_host format: {user_host}")
        user, host = parts
        conn = SSHConnection(host, user, password)
        self._pool[user_host] = conn
        return conn

    def clear(self, user_host: str) -> None:
        if user_host in self._pool:
            self._pool[user_host].close()
            del self._pool[user_host]

    def clear_all(self) -> None:
        for conn in self._pool.values():
            conn.close()
        self._pool.clear()
