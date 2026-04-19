"""
测试套件
- 本地 patch、rollback、backup 测试
- 兼容性检查和重叠检测测试
- 远程操作 mock 测试
"""

import os
import shutil
import tempfile
import pytest
from unittest.mock import MagicMock, patch

from lib.fs import LocalFS, RemoteFS, parse_path
from lib.compat import check_patch_compatibility, find_overlapping_paths, backup_overlapping_files, CompatStatus
from lib.operations import PatchOperation, RollbackOperation, BackupOperation
from lib.ssh_client import SSHPool, AuthenticationError, ConnectionTimeoutError, SSHConnection


class TestLocalFS:
    def test_listdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            os.makedirs(os.path.join(tmpdir, "a", "b"))
            with open(os.path.join(tmpdir, "file.txt"), "w") as f:
                f.write("hello")
            items = fs.listdir(tmpdir)
            assert "a" in items
            assert "file.txt" in items
            assert ".hidden" not in items

    def test_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            assert fs.exists(tmpdir)
            assert not fs.exists(os.path.join(tmpdir, "nonexist"))

    def test_copy_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            src = os.path.join(tmpdir, "src.txt")
            dst = os.path.join(tmpdir, "dst.txt")
            with open(src, "w") as f:
                f.write("content")
            fs.copy(src, dst)
            assert os.path.exists(dst)
            with open(dst) as f:
                assert f.read() == "content"

    def test_copy_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            src = os.path.join(tmpdir, "src")
            dst = os.path.join(tmpdir, "dst")
            os.makedirs(os.path.join(src, "sub"))
            with open(os.path.join(src, "sub", "file.txt"), "w") as f:
                f.write("hello")
            fs.copy(src, dst)
            assert os.path.exists(os.path.join(dst, "sub", "file.txt"))

    def test_remove(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            path = os.path.join(tmpdir, "del_me")
            os.makedirs(path)
            fs.remove(path)
            assert not os.path.exists(path)


class TestParsePath:
    def test_local_relative(self):
        is_remote, user, host, real_path = parse_path("foo/bar")
        assert not is_remote
        assert real_path == os.path.abspath("foo/bar")

    def test_local_absolute(self):
        is_remote, user, host, real_path = parse_path("/foo/bar")
        assert not is_remote
        assert real_path == "/foo/bar"

    def test_local_tilde(self):
        is_remote, user, host, real_path = parse_path("~/foo")
        assert not is_remote
        assert real_path == os.path.expanduser("~/foo")

    def test_remote(self):
        is_remote, user, host, real_path = parse_path("user@host:/path/to/dir")
        assert is_remote
        assert user == "user"
        assert host == "host"
        assert real_path == "/path/to/dir"


class TestCompatibility:
    def test_empty_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            src = os.path.join(tmpdir, "src")
            dst = os.path.join(tmpdir, "dst")
            os.makedirs(src)
            status = check_patch_compatibility(fs, fs, src, dst)
            assert status == CompatStatus.EMPTY_TARGET

    def test_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            src = os.path.join(tmpdir, "src")
            dst = os.path.join(tmpdir, "dst")
            os.makedirs(os.path.join(src, "a"))
            os.makedirs(os.path.join(dst, "a"))
            status = check_patch_compatibility(fs, fs, src, dst)
            assert status == CompatStatus.MATCH

    def test_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            src = os.path.join(tmpdir, "src")
            dst = os.path.join(tmpdir, "dst")
            os.makedirs(os.path.join(src, "a"))
            os.makedirs(os.path.join(src, "b"))
            os.makedirs(os.path.join(dst, "a"))
            os.makedirs(os.path.join(dst, "c"))
            status = check_patch_compatibility(fs, fs, src, dst)
            assert status == CompatStatus.PARTIAL

    def test_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            src = os.path.join(tmpdir, "src")
            dst = os.path.join(tmpdir, "dst")
            os.makedirs(os.path.join(src, "a"))
            os.makedirs(os.path.join(dst, "b"))
            status = check_patch_compatibility(fs, fs, src, dst)
            assert status == CompatStatus.NONE


class TestOverlap:
    def test_find_overlapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            src = os.path.join(tmpdir, "src")
            dst = os.path.join(tmpdir, "dst")
            os.makedirs(os.path.join(src, "a"))
            os.makedirs(os.path.join(src, "b"))
            os.makedirs(os.path.join(dst, "a"))
            overlapping = find_overlapping_paths(fs, fs, src, dst)
            assert "a" in overlapping
            assert "b" not in overlapping

    def test_backup_overlapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            src = os.path.join(tmpdir, "src")
            dst = os.path.join(tmpdir, "dst")
            backup = os.path.join(tmpdir, "backup")
            os.makedirs(os.path.join(src, "a"))
            os.makedirs(os.path.join(dst, "a"))
            os.makedirs(os.path.join(dst, "b"))

            import logging
            logger = logging.getLogger("test")
            logger.setLevel(logging.DEBUG)

            name = backup_overlapping_files(fs, fs, src, dst, fs, backup, logger)
            assert name is not None
            assert os.path.exists(os.path.join(backup, name, "a"))
            assert not os.path.exists(os.path.join(backup, name, "b"))


class TestOperations:
    def test_patch_local(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            output = os.path.join(tmpdir, "output")
            target = os.path.join(tmpdir, "target")
            os.makedirs(os.path.join(output, "sub"))
            with open(os.path.join(output, "sub", "file.txt"), "w") as f:
                f.write("new")

            import logging
            logger = logging.getLogger("test")
            logger.setLevel(logging.DEBUG)

            op = PatchOperation(fs, fs, output, target, logger)
            result = op.run()
            assert result.success
            assert os.path.exists(os.path.join(target, "sub", "file.txt"))
            with open(os.path.join(target, "sub", "file.txt")) as f:
                assert f.read() == "new"

    def test_backup_local(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            target = os.path.join(tmpdir, "target")
            backup_dir = os.path.join(tmpdir, "backup")
            os.makedirs(os.path.join(target, "sub"))
            with open(os.path.join(target, "file.txt"), "w") as f:
                f.write("data")

            import logging
            logger = logging.getLogger("test")
            logger.setLevel(logging.DEBUG)

            op = BackupOperation(fs, fs, target, backup_dir, logger)
            result = op.run()
            assert result.success
            assert os.path.exists(backup_dir)
            backups = os.listdir(backup_dir)
            assert len(backups) == 1

    def test_rollback_local(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            backup = os.path.join(tmpdir, "backup_src")
            target = os.path.join(tmpdir, "target")
            os.makedirs(os.path.join(backup, "sub"))
            with open(os.path.join(backup, "sub", "file.txt"), "w") as f:
                f.write("old")

            import logging
            logger = logging.getLogger("test")
            logger.setLevel(logging.DEBUG)

            op = RollbackOperation(fs, fs, backup, target, logger)
            result = op.run()
            assert result.success
            with open(os.path.join(target, "sub", "file.txt")) as f:
                assert f.read() == "old"

    def test_patch_cancel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fs = LocalFS()
            output = os.path.join(tmpdir, "output")
            target = os.path.join(tmpdir, "target")
            os.makedirs(os.path.join(output, "sub"))
            with open(os.path.join(output, "sub", "file.txt"), "w") as f:
                f.write("new")

            import logging
            logger = logging.getLogger("test")
            logger.setLevel(logging.DEBUG)

            cancelled = True
            op = PatchOperation(fs, fs, output, target, logger, cancelled_callback=lambda: cancelled)
            result = op.run()
            assert not result.success
            assert "cancelled" in result.message.lower() or "cancel" in result.message.lower()


class TestRemoteFSMock:
    def test_listdir_mock(self):
        mock_sftp = MagicMock()
        mock_sftp.listdir.return_value = ["file1.txt", ".hidden", "dir1"]
        mock_stat = MagicMock()
        mock_stat.st_mode = 0o040755
        mock_sftp.stat.side_effect = lambda p: mock_stat if "dir1" in p else MagicMock(st_mode=0o100644)

        mock_conn = MagicMock()
        mock_conn.sftp = mock_sftp

        fs = RemoteFS(mock_conn)
        items = fs.listdir("/remote/path")
        assert "file1.txt" in items
        assert "dir1" in items
        assert ".hidden" not in items

    def test_exists_mock(self):
        mock_sftp = MagicMock()
        mock_sftp.stat.return_value = MagicMock()

        mock_conn = MagicMock()
        mock_conn.sftp = mock_sftp

        fs = RemoteFS(mock_conn)
        assert fs.exists("/remote/path")
        mock_sftp.stat.side_effect = IOError()
        assert not fs.exists("/remote/missing")


class TestSSHPool:
    def test_clear_nonexistent(self):
        pool = SSHPool()
        pool.clear("user@host")

    @patch("lib.ssh_client.paramiko.SSHClient")
    def test_get_caches_connection(self, mock_ssh_class):
        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_client.get_transport.return_value = mock_transport
        mock_ssh_class.return_value = mock_client

        mock_sftp = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp
        mock_channel = MagicMock()
        mock_sftp.get_channel.return_value = mock_channel

        pool = SSHPool()
        conn = pool.get("user@host", "password")
        assert conn is not None
        assert "user@host" in pool._pool

        # 再次获取应返回缓存
        conn2 = pool.get("user@host", "password")
        assert conn2 is conn

    @patch("lib.ssh_client.paramiko.SSHClient")
    def test_auth_failure(self, mock_ssh_class):
        mock_client = MagicMock()
        mock_client.connect.side_effect = Exception("auth failed")
        mock_ssh_class.return_value = mock_client

        pool = SSHPool()
        with pytest.raises(Exception):
            pool.get("user@host", "wrong_password")


class TestSSHProxyConnection:
    """测试 SSH 跳板连接功能"""

    @patch("lib.ssh_client.paramiko.SSHClient")
    def test_open_proxy_connection_success(self, mock_ssh_class):
        """测试成功建立跳板连接"""
        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_client.get_transport.return_value = mock_transport
        mock_ssh_class.return_value = mock_client

        # 模拟 direct-tcpip 通道
        mock_channel = MagicMock()
        mock_transport.open_channel.return_value = mock_channel

        conn = SSHConnection("gateway", "user", "password")
        proxy_conn = conn.open_proxy_connection("internal", "target_user", "target_password")

        assert proxy_conn is not None
        mock_transport.open_channel.assert_called_once_with(
            "direct-tcpip", ("internal", 22), ("127.0.0.1", 0)
        )
        # 验证第二个 SSHClient 被创建时传入了 sock=mock_channel
        assert mock_ssh_class.call_count == 2

    @patch("lib.ssh_client.paramiko.SSHClient")
    def test_open_proxy_connection_transport_inactive(self, mock_ssh_class):
        """测试 transport 断开时抛出异常"""
        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = False
        mock_client.get_transport.return_value = mock_transport
        mock_ssh_class.return_value = mock_client

        conn = SSHConnection("gateway", "user", "password")
        with pytest.raises(ConnectionTimeoutError):
            conn.open_proxy_connection("internal", "target_user", "target_password")

    @patch("lib.ssh_client.paramiko.SSHClient")
    def test_open_proxy_connection_channel_none(self, mock_ssh_class):
        """测试 direct-tcpip 通道打开失败"""
        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_transport.open_channel.return_value = None
        mock_client.get_transport.return_value = mock_transport
        mock_ssh_class.return_value = mock_client

        conn = SSHConnection("gateway", "user", "password")
        with pytest.raises(ConnectionTimeoutError) as exc_info:
            conn.open_proxy_connection("internal", "target_user", "target_password")
        assert "Failed to open proxy channel" in str(exc_info.value)


class TestSFTPStreamingCopy:
    """测试 Remote->Remote SFTP 流式传输"""

    def test_copy_file_remote_to_remote_streaming(self):
        """测试 PatchOperation Remote->Remote 使用流式传输而非本地中转"""
        mock_sftp_src = MagicMock()
        mock_file_obj = MagicMock()
        mock_sftp_src.open.return_value = mock_file_obj

        mock_sftp_dst = MagicMock()

        mock_conn_src = MagicMock()
        mock_conn_src.sftp = mock_sftp_src
        mock_conn_src.user = "testuser"

        mock_conn_dst = MagicMock()
        mock_conn_dst.sftp = mock_sftp_dst
        mock_conn_dst.user = "testuser"

        src_fs = RemoteFS(mock_conn_src)
        dst_fs = RemoteFS(mock_conn_dst)

        import logging
        logger = logging.getLogger("test")
        logger.setLevel(logging.DEBUG)

        op = PatchOperation(src_fs, dst_fs, "/src/file.txt", "/dst/file.txt", logger)
        op._copy_file("/src/file.txt", "/dst/file.txt")

        # 验证使用了 sftp.open + putfo，没有使用 download/upload
        mock_sftp_src.open.assert_called_once_with("/src/file.txt", "rb")
        mock_sftp_dst.putfo.assert_called_once_with(mock_file_obj, "/dst/file.txt")
        mock_file_obj.close.assert_called_once()

    def test_copy_file_remote_to_remote_streaming_with_tilde(self):
        """测试 PatchOperation Remote->Remote 带 ~ 路径时正确解析"""
        mock_sftp_src = MagicMock()
        mock_file_obj = MagicMock()
        mock_sftp_src.open.return_value = mock_file_obj

        mock_sftp_dst = MagicMock()

        mock_conn_src = MagicMock()
        mock_conn_src.sftp = mock_sftp_src
        mock_conn_src.user = "testuser"

        mock_conn_dst = MagicMock()
        mock_conn_dst.sftp = mock_sftp_dst
        mock_conn_dst.user = "testuser"

        src_fs = RemoteFS(mock_conn_src)
        dst_fs = RemoteFS(mock_conn_dst)
        # 显式设置 home_dir 以覆盖 _get_home_dir 的默认值
        src_fs._home_dir = "/home/testuser"
        dst_fs._home_dir = "/home/testuser"

        import logging
        logger = logging.getLogger("test")
        logger.setLevel(logging.DEBUG)

        op = PatchOperation(src_fs, dst_fs, "~/src/file.txt", "~/dst/file.txt", logger)
        op._copy_file("~/src/file.txt", "~/dst/file.txt")

        # 验证 ~ 被解析为 /home/testuser
        mock_sftp_src.open.assert_called_once_with("/home/testuser/src/file.txt", "rb")
        mock_sftp_dst.putfo.assert_called_once_with(mock_file_obj, "/home/testuser/dst/file.txt")
        mock_file_obj.close.assert_called_once()

    def test_copy_between_fs_remote_to_remote_streaming(self):
        """测试 _copy_between_fs Remote->Remote 使用流式传输"""
        mock_sftp_src = MagicMock()
        mock_file_obj = MagicMock()
        mock_sftp_src.open.return_value = mock_file_obj
        mock_sftp_src.stat.side_effect = lambda p: MagicMock(st_mode=0o100644)

        mock_sftp_dst = MagicMock()

        mock_conn_src = MagicMock()
        mock_conn_src.sftp = mock_sftp_src
        mock_conn_src.user = "testuser"

        mock_conn_dst = MagicMock()
        mock_conn_dst.sftp = mock_sftp_dst
        mock_conn_dst.user = "testuser"

        src_fs = RemoteFS(mock_conn_src)
        dst_fs = RemoteFS(mock_conn_dst)

        import logging
        logger = logging.getLogger("test")

        from lib.compat import _copy_between_fs
        _copy_between_fs(src_fs, "/src/file.txt", dst_fs, "/dst/file.txt", logger)

        mock_sftp_src.open.assert_called_once_with("/src/file.txt", "rb")
        mock_sftp_dst.putfo.assert_called_once_with(mock_file_obj, "/dst/file.txt")
        mock_file_obj.close.assert_called_once()

    def test_copy_between_fs_remote_to_remote_streaming_with_tilde(self):
        """测试 _copy_between_fs Remote->Remote 带 ~ 路径时正确解析"""
        mock_sftp_src = MagicMock()
        mock_file_obj = MagicMock()
        mock_sftp_src.open.return_value = mock_file_obj
        mock_sftp_src.stat.side_effect = lambda p: MagicMock(st_mode=0o100644)

        mock_sftp_dst = MagicMock()

        mock_conn_src = MagicMock()
        mock_conn_src.sftp = mock_sftp_src
        mock_conn_src.user = "testuser"

        mock_conn_dst = MagicMock()
        mock_conn_dst.sftp = mock_sftp_dst
        mock_conn_dst.user = "testuser"

        src_fs = RemoteFS(mock_conn_src)
        dst_fs = RemoteFS(mock_conn_dst)
        src_fs._home_dir = "/home/testuser"
        dst_fs._home_dir = "/home/testuser"

        import logging
        logger = logging.getLogger("test")

        from lib.compat import _copy_between_fs
        _copy_between_fs(src_fs, "~/src/file.txt", dst_fs, "~/dst/file.txt", logger)

        mock_sftp_src.open.assert_called_once_with("/home/testuser/src/file.txt", "rb")
        mock_sftp_dst.putfo.assert_called_once_with(mock_file_obj, "/home/testuser/dst/file.txt")
        mock_file_obj.close.assert_called_once()


class TestTargetViaOutputValidation:
    """测试 Via Output Host 边界校验逻辑"""

    def test_parse_path_remote_with_port_like_host(self):
        """测试带端口的主机名解析行为"""
        # 当前 parse_path 从 @ 后找第一个 :，所以 user@gateway:2222:/path
        # 会被解析为 host="gateway", real_path="2222:/path"
        # 这是一个已知的边界约束：不支持自定义端口
        is_remote, user, host, real_path = parse_path("user@gateway:2222:/path")
        assert is_remote
        assert user == "user"
        assert host == "gateway"
        assert real_path == "2222:/path"

    def test_parse_path_edge_cases(self):
        """测试路径解析边界情况"""
        # 只有 @ 没有 :
        is_remote, user, host, real_path = parse_path("user@host/path")
        assert not is_remote  # 没有 : 分隔符，视为本地路径

        # 空的远程路径
        is_remote, user, host, real_path = parse_path("user@host:")
        assert is_remote
        assert real_path == ""


class TestWorkerThreadTargetViaOutput:
    """测试 WorkerThread 的 _get_target_fs 逻辑"""

    def test_get_target_fs_without_via_output(self):
        """测试未勾选 Via Output Host 时走常规路径"""
        from gui.thread import WorkerThread

        thread = WorkerThread("patch", {"output": "/local", "target": "/local/target"}, None, {}, target_via_output=False)
        fs, real_path = thread._get_fs("/local/target")
        assert isinstance(fs, LocalFS)
        assert real_path == "/local/target"

    @patch("lib.ssh_client.paramiko.SSHClient")
    def test_get_target_fs_with_via_output_success(self, mock_ssh_class):
        """测试勾选 Via Output Host 时正确建立跳板连接"""
        from gui.thread import WorkerThread

        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_client.get_transport.return_value = mock_transport
        mock_ssh_class.return_value = mock_client

        mock_channel = MagicMock()
        mock_transport.open_channel.return_value = mock_channel

        mock_sftp = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp
        mock_sftp_channel = MagicMock()
        mock_sftp.get_channel.return_value = mock_sftp_channel

        # 创建 mock config，包含两个密码
        mock_config = MagicMock()
        mock_config.ssh_passwords = {
            "user@gateway": "gateway_pass",
            "user@internal": "internal_pass"
        }

        ssh_pool = SSHPool()

        thread = WorkerThread(
            "patch",
            {"output": "user@gateway:/data/output", "target": "user@internal:/data/target"},
            ssh_pool,
            mock_config,
            target_via_output=True
        )

        target_fs, target_real = thread._get_target_fs()
        assert isinstance(target_fs, RemoteFS)
        assert target_real == "/data/target"

    @patch("lib.ssh_client.paramiko.SSHClient")
    def test_rollback_get_target_fs_with_via_output_success(self, mock_ssh_class):
        """测试 rollback 勾选 Via Output Host 时正确建立跳板连接（回归：paths 需包含 output）"""
        from gui.thread import WorkerThread

        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_client.get_transport.return_value = mock_transport
        mock_ssh_class.return_value = mock_client

        mock_channel = MagicMock()
        mock_transport.open_channel.return_value = mock_channel

        mock_sftp = MagicMock()
        mock_client.open_sftp.return_value = mock_sftp
        mock_sftp_channel = MagicMock()
        mock_sftp.get_channel.return_value = mock_sftp_channel

        mock_config = MagicMock()
        mock_config.ssh_passwords = {
            "user@gateway": "gateway_pass",
            "user@internal": "internal_pass"
        }

        ssh_pool = SSHPool()

        # rollback 时 paths 必须包含 output，否则 _get_target_fs 会抛 KeyError
        thread = WorkerThread(
            "rollback",
            {
                "backup": "user@gateway:/data/backup",
                "target": "user@internal:/data/target",
                "backup_dir": "user@gateway:/data/backup",
                "output": "user@gateway:/data/output",
            },
            ssh_pool,
            mock_config,
            target_via_output=True
        )

        target_fs, target_real = thread._get_target_fs()
        assert isinstance(target_fs, RemoteFS)
        assert target_real == "/data/target"

    def test_get_target_fs_output_local_raises(self):
        """测试 output 为本地路径时抛出 ValueError"""
        from gui.thread import WorkerThread

        mock_config = MagicMock()
        mock_config.ssh_passwords = {}

        thread = WorkerThread(
            "patch",
            {"output": "/local/output", "target": "user@internal:/data/target"},
            None,
            mock_config,
            target_via_output=True
        )

        with pytest.raises(ValueError) as exc_info:
            thread._get_target_fs()
        assert "Output must be a remote path" in str(exc_info.value)

    def test_get_target_fs_target_local_raises(self):
        """测试 target 为本地路径时抛出 ValueError"""
        from gui.thread import WorkerThread

        mock_config = MagicMock()
        mock_config.ssh_passwords = {"user@gateway": "pass"}

        thread = WorkerThread(
            "patch",
            {"output": "user@gateway:/data/output", "target": "/local/target"},
            None,
            mock_config,
            target_via_output=True
        )

        with pytest.raises(ValueError) as exc_info:
            thread._get_target_fs()
        assert "Target must be a remote path" in str(exc_info.value)

    def test_get_target_fs_missing_password_raises(self):
        """测试缺少密码时抛出 AuthenticationError"""
        from gui.thread import WorkerThread

        mock_config = MagicMock()
        mock_config.ssh_passwords = {}  # 没有密码

        thread = WorkerThread(
            "patch",
            {"output": "user@gateway:/data/output", "target": "user@internal:/data/target"},
            None,
            mock_config,
            target_via_output=True
        )

        with pytest.raises(AuthenticationError) as exc_info:
            thread._get_target_fs()
        assert "AUTH:user@gateway" in str(exc_info.value)


class TestPreCheckThreadTargetViaOutput:
    """测试 PreCheckThread 的 _get_target_fs 逻辑"""

    def test_get_target_fs_without_via_output(self):
        """测试未勾选 Via Output Host 时走常规路径"""
        from gui.thread import PreCheckThread

        thread = PreCheckThread("/local/output", "/local/target", None, {}, target_via_output=False)
        fs, real_path = thread._get_fs("/local/target")
        assert isinstance(fs, LocalFS)
        assert real_path == "/local/target"

    def test_get_target_fs_output_local_raises(self):
        """测试 output 为本地路径时抛出 ValueError"""
        from gui.thread import PreCheckThread

        mock_config = MagicMock()
        mock_config.ssh_passwords = {}

        thread = PreCheckThread(
            "/local/output",
            "user@internal:/data/target",
            None,
            mock_config,
            target_via_output=True
        )

        with pytest.raises(ValueError) as exc_info:
            thread._get_target_fs()
        assert "Output must be a remote path" in str(exc_info.value)
