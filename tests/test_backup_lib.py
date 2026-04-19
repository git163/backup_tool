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
from lib.ssh_client import SSHPool, AuthenticationError


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
