"""
兼容性检查与重叠检测
- check_patch_compatibility: 比较源目录和目标目录的顶层结构
- find_overlapping_paths: 检测将被覆盖的文件/目录
- backup_overlapping_files: 仅备份会被覆盖的部分
"""

import os
from enum import Enum
from typing import Optional

from lib.fs import FileSystem, RemoteFS, TempLocalFS


class CompatStatus(Enum):
    MATCH = "match"
    PARTIAL = "partial"
    NONE = "none"
    EMPTY_TARGET = "empty_target"
    REMOTE = "remote"


def check_patch_compatibility(
    source_fs: FileSystem,
    target_fs: FileSystem,
    source_path: str,
    target_path: str,
) -> CompatStatus:
    """比较源目录和目标目录的顶层结构。"""
    if not source_fs.exists(source_path):
        raise FileNotFoundError(f"Source not found: {source_path}")

    if not target_fs.exists(target_path):
        return CompatStatus.EMPTY_TARGET

    source_items = set(source_fs.listdir(source_path))
    target_items = set(target_fs.listdir(target_path))

    if not source_items:
        return CompatStatus.EMPTY_TARGET

    if not target_items:
        return CompatStatus.EMPTY_TARGET

    overlap = source_items & target_items

    if overlap == source_items:
        return CompatStatus.MATCH
    elif overlap:
        return CompatStatus.PARTIAL
    else:
        return CompatStatus.NONE


def find_overlapping_paths(
    source_fs: FileSystem,
    target_fs: FileSystem,
    source_path: str,
    target_path: str,
) -> list[str]:
    """
    返回将被覆盖的文件所在的最底层目录列表，去重。
    路径相对于 source_path/target_path。
    """
    # 忽略的文件名（完全匹配）和后缀
    IGNORE_EXACT = {".DS_Store", "thumbs.db"}
    IGNORE_SUFFIX = {".pyc"}

    if not target_fs.exists(target_path):
        return []

    dirs_with_files = set()

    def collect_dirs(fs: FileSystem, base_path: str, rel_prefix: str = ""):
        if not fs.exists(base_path):
            return
        for item in fs.listdir(base_path):
            # 跳过忽略的文件和目录
            if item in IGNORE_EXACT or any(item.endswith(s) for s in IGNORE_SUFFIX):
                continue

            item_path = fs.join(base_path, item)
            rel_path = f"{rel_prefix}/{item}" if rel_prefix else item

            if fs.isdir(item_path):
                collect_dirs(fs, item_path, rel_path)
            else:
                # 文件，找到其所在目录
                parent = fs.dirname(rel_path)
                if parent:
                    dirs_with_files.add(parent)
                else:
                    # 根目录下的文件
                    dirs_with_files.add("")

    collect_dirs(source_fs, source_path)

    # 过滤出在 target 中存在的目录
    result = []
    for dir_path in dirs_with_files:
        target_dir = target_fs.join(target_path, dir_path) if dir_path else target_path
        if target_fs.exists(target_dir):
            result.append(dir_path if dir_path else ".")

    return result


def backup_overlapping_files(
    source_fs: FileSystem,
    target_fs: FileSystem,
    source_path: str,
    target_path: str,
    backup_fs: FileSystem,
    backup_dir: str,
    logger,
    cancelled_callback=None,
) -> Optional[str]:
    """
    仅备份会被覆盖的部分。
    返回备份子目录名（如 target_YYYYMMDD_HHMMSS），若无需备份返回 None。
    远程目标则完整备份。
    """
    overlapping = find_overlapping_paths(source_fs, target_fs, source_path, target_path)
    if not overlapping:
        return None

    from datetime import datetime
    basename = target_fs.basename(target_path) or "target"
    backup_name = f"{basename}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_subdir = backup_fs.join(backup_dir, backup_name)
    backup_fs.makedirs(backup_subdir, exist_ok=True)

    for name in overlapping:
        if cancelled_callback and cancelled_callback():
            raise RuntimeError("Operation cancelled")
        src = target_fs.join(target_path, name)
        dst = backup_fs.join(backup_subdir, name)
        logger.info(f"Backup: {src} -> {dst}")
        _copy_between_fs(target_fs, src, backup_fs, dst, logger, cancelled_callback)

    return backup_name


def _copy_between_fs(
    src_fs: FileSystem,
    src_path: str,
    dst_fs: FileSystem,
    dst_path: str,
    logger,
    cancelled_callback=None,
) -> None:
    """在两个 FileSystem 之间复制文件或目录。"""
    if cancelled_callback and cancelled_callback():
        raise RuntimeError("Operation cancelled")

    if src_fs.isfile(src_path):
        if isinstance(src_fs, RemoteFS) and isinstance(dst_fs, RemoteFS):
            # Remote -> Remote (SFTP 流式中转)
            src_file = src_fs.sftp.open(src_fs._resolve(src_path), 'rb')
            try:
                dst_fs.sftp.putfo(src_file, dst_fs._resolve(dst_path))
            finally:
                src_file.close()
        elif isinstance(src_fs, RemoteFS):
            # Remote -> Local
            src_fs.download_file(src_path, dst_path)
        elif isinstance(dst_fs, RemoteFS):
            # Local -> Remote
            dst_fs.upload_file(src_path, dst_path)
        else:
            # Local -> Local
            dst_fs.copy(src_path, dst_path)
    elif src_fs.isdir(src_path):
        dst_fs.makedirs(dst_path, exist_ok=True)
        for name in src_fs.listdir(src_path):
            _copy_between_fs(
                src_fs, src_fs.join(src_path, name),
                dst_fs, dst_fs.join(dst_path, name),
                logger, cancelled_callback
            )
