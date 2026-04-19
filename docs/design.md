# Auto Backup and Patch Tool — 详细设计文档

## 1. 设计目标

- **跨平台**：macOS / Windows 客户端，目标为 Linux 远程目录
- **统一抽象**：本地与远程文件操作使用同一套接口，上层无感知
- **GUI 最薄**：所有业务逻辑下沉到 `lib/`，`gui/` 只做界面与信号转发
- **可测试**：核心逻辑不依赖 Qt，可用 pytest + mock 覆盖

---

## 2. 模块划分

```
lib/
  __init__.py
  fs.py          — 文件系统抽象（LocalFS + RemoteFS）
  ssh_client.py  — SSH 连接池与密码管理
  operations.py  — Patch / Rollback / Backup 操作类
  compat.py      — 兼容性检查、重叠检测、选择性备份
  config.py      — JSON 配置读写
  logger.py      — 按天日志 + Qt Signal 桥接
gui/
  __init__.py
  app.py         — PySide2/PySide6 兼容入口
  main_window.py — 主窗口布局与信号绑定
  dialogs.py     — 密码框、远程目录浏览器、Markdown 确认对话框
  thread.py      — 后台工作线程（预检 + 执行）
```

---

## 3. 核心类设计

### 3.1 文件系统抽象 (`lib/fs.py`)

```python
class FileSystem(ABC):
    @abstractmethod
    def listdir(self, path: str) -> list[str]: ...
    @abstractmethod
    def exists(self, path: str) -> bool: ...
    @abstractmethod
    def isdir(self, path: str) -> bool: ...
    @abstractmethod
    def isfile(self, path: str) -> bool: ...
    @abstractmethod
    def mkdir(self, path: str, exist_ok: bool = False) -> None: ...
    @abstractmethod
    def copy(self, src: str, dst: str) -> None: ...       # 文件或目录递归复制
    @abstractmethod
    def remove(self, path: str) -> None: ...              # 文件或目录递归删除
    @abstractmethod
    def join(self, *parts: str) -> str: ...

class LocalFS(FileSystem):
    """基于 pathlib / shutil 的本地文件系统实现。"""

class RemoteFS(FileSystem):
    """
    基于 paramiko.SFTPClient 的远程文件系统实现。
    内部持有 ssh_client.SSHConnection，路径按 Linux 规则处理。
    """
```

**设计理由**：上层 `operations.py` 和 `compat.py` 只面对 `FileSystem` 接口，无需判断路径是本地还是远程。

### 3.2 SSH 连接与密码管理 (`lib/ssh_client.py`)

```python
CONNECTION_TIMEOUT = 10      # SSH 连接超时（秒）
AUTH_TIMEOUT = 10            # 密码验证超时（秒）
COMMAND_TIMEOUT = 30         # 远程命令执行超时（秒）
SFTP_TIMEOUT = 60            # SFTP 单次读写超时（秒）
TRANSFER_CHUNK_SIZE = 32768  # 传输分块大小（字节）

class AuthenticationError(Exception): ...
class ConnectionTimeoutError(Exception): ...
class SFTPTimeoutError(Exception): ...

class SSHConnection:
    """封装 paramiko.SSHClient + SFTPClient，支持复用。"""
    host: str
    user: str
    client: paramiko.SSHClient
    sftp: paramiko.SFTPClient
    def verify_password(self, password: str) -> bool: ...
    def close(self) -> None: ...

class SSHPool:
    """
    连接池：按 user@host 缓存 SSHConnection。
    未缓存时抛出 AuthenticationError，由 GUI 捕获并弹窗。
    所有连接和操作均带超时控制。
    """
    _pool: dict[str, SSHConnection]
    def get(self, user_host: str, password: str | None = None) -> SSHConnection: ...
    def clear(self, user_host: str) -> None: ...
```

**密码流程**：
1. `SSHPool.get()` 先从 `config.json` 的 `ssh_passwords` 查找密码
2. 若无缓存或验证失败 → 抛出 `AuthenticationError`
3. GUI 捕获后弹出 `PasswordDialog`
4. 用户输入 → 调用 `verify_password()` → 成功则写回 `config.json` → 重试操作

**超时机制**：
- `paramiko.Transport` 设置 `banner_timeout=CONNECTION_TIMEOUT`, `auth_timeout=AUTH_TIMEOUT`
- `SSHClient.connect()` 设置 `timeout=CONNECTION_TIMEOUT`
- `SFTPClient.get_channel().settimeout(SFTP_TIMEOUT)` 控制读写超时
- 远程命令通过 `channel.settimeout(COMMAND_TIMEOUT)` 控制
- 大文件传输分块进行，每块写入后检查取消标志，避免无限等待

### 3.3 操作类 (`lib/operations.py`)

```python
class OperationResult(NamedTuple):
    success: bool
    message: str

class BaseOperation(ABC):
    source_fs: FileSystem
    target_fs: FileSystem
    logger: logging.Logger
    progress_callback: Callable[[str, str], None] | None = None

    @abstractmethod
    def run(self) -> OperationResult: ...

class PatchOperation(BaseOperation):
    """将 source 的内容复制到 target，覆盖已有文件。"""
    def run(self) -> OperationResult: ...

class RollbackOperation(BaseOperation):
    """将 backup 恢复到 target。"""
    def run(self) -> OperationResult: ...

class BackupOperation(BaseOperation):
    """将 target 完整备份到 backup_dir，命名格式 {basename}_YYYYMMDD_HHMMSS。"""
    backup_dir: str
    def run(self) -> OperationResult: ...
```

**设计理由**：操作类持有两个 `FileSystem` 实例，天然支持四种组合（本地↔本地、本地↔远程、远程↔本地、远程↔远程）。远程→远程通过临时中转（先下载到本地 temp，再上传）。

### 3.4 兼容性与重叠检测 (`lib/compat.py`)

```python
class CompatStatus(Enum):
    MATCH = "match"
    PARTIAL = "partial"
    NONE = "none"
    EMPTY_TARGET = "empty_target"
    REMOTE = "remote"

def check_patch_compatibility(
    source_fs: FileSystem, target_fs: FileSystem,
    source_path: str, target_path: str
) -> CompatStatus:
    """比较源目录和目标目录的顶层结构。"""

def find_overlapping_paths(
    source_fs: FileSystem, target_fs: FileSystem,
    source_path: str, target_path: str
) -> list[str]:
    """返回将被覆盖的文件/目录列表，仅保留最底层项。"""

def backup_overlapping_files(
    source_fs: FileSystem, target_fs: FileSystem,
    source_path: str, target_path: str,
    backup_fs: FileSystem, backup_dir: str,
    logger: logging.Logger
) -> str | None:
    """仅备份会被覆盖的部分，返回备份子目录名（远程目标则完整备份）。"""
```

### 3.5 配置 (`lib/config.py`)

```python
class Config:
    data: dict
    def load(self, path: str) -> None: ...
    def save(self, path: str) -> None: ...
    def get(self, key: str, default=None): ...
    def set(self, key: str, value): ...
    @property
    def ssh_passwords(self) -> dict[str, str]: ...
```

### 3.6 日志 (`lib/logger.py`)

```python
class QtLogHandler(logging.Handler):
    """自定义 Handler，通过 Qt Signal 将日志发送到 GUI。"""
    log_signal: pyqtSignal(str)

class AppLogger:
    """
    统一管理：
    - 文件 Handler：按天存储到 logs/YYYY-MM-DD.log
    - Qt Handler：供 GUI 实时显示
    """
    @staticmethod
    def setup(name: str = "app") -> logging.Logger: ...
```

---

## 4. GUI 设计

### 4.1 主窗口 (`gui/main_window.py`)

```python
class MainWindow(QMainWindow):
    # 三个目录输入框（QLineEdit），支持 ~ 和 user@host:/path
    backup_edit: QLineEdit
    output_edit: QLineEdit
    target_edit: QLineEdit

    # 浏览按钮：本地路径 → QFileDialog；远程路径 → RemoteDirDialog
    # 操作按钮：Patch / Rollback
    # 日志区：QTextEdit（只读）
    # 菜单：File → Save Config / Load Config / Exit
```

### 4.2 对话框 (`gui/dialogs.py`)

```python
class PasswordDialog(QDialog):
    """user_host 标签 + 密码输入 + 记住密码复选框。"""
    def get_password(self) -> tuple[str, bool]: ...  # (password, remember)

class RemoteDirDialog(QDialog):
    """
    SSH 远程目录浏览器。
    QListView + 当前路径 QLabel + 返回上级 / 刷新。
    双击进入目录，单击选中。异步加载（不阻塞）。
    """
    selected_path: str  # 用户最终选中的完整路径

class ConfirmDialog(QDialog):
    """
    QTextBrowser 渲染 Markdown 表格。
    展示兼容性状态和将被覆盖的文件列表（最多 20 项）。
    Yes / No 按钮。
    """
```

### 4.3 后台线程 (`gui/thread.py`)

```python
class PreCheckThread(QThread):
    """
    输入：source_path, target_path, config
    执行：创建 FileSystem → check_patch_compatibility → find_overlapping_paths
    信号：result(status, overlapping_paths) / error(msg) / log(msg)
    """

class WorkerThread(QThread):
    """
    输入：operation_type, paths, config
    执行：创建 FileSystem → 备份重叠文件 → 执行 Operation.run()
    信号：progress(step, detail) / log(msg) / finished(result) / error(msg)
    """
    _cancelled: bool = False
    def cancel(self) -> None: ...
```

**防卡死设计**：
- **所有阻塞操作必须在 QThread 中执行**：SSH 连接、SFTP 传输、文件复制、兼容性检查全部放到 `PreCheckThread` 或 `WorkerThread`，主线程始终保持 Qt 事件循环响应
- **操作期间忙状态**：启动线程时禁用 Patch/Rollback/浏览按钮，设置 `QApplication.setOverrideCursor(Qt.WaitCursor)`，操作完成后恢复
- **支持取消**：`WorkerThread` 内置 `_cancelled` 标志，每传输一个文件前检查；若取消，清理临时文件后安全退出
- **远程目录浏览器**：`RemoteDirDialog` 内部使用 `QThread` 加载目录列表，加载过程中显示 "Loading..."，不阻塞对话框
- **超时兜底**：线程内捕获 `ConnectionTimeoutError` / `SFTPTimeoutError`，通过 error 信号回传 GUI，由主线程弹对话框提示

---

## 5. 关键流程

### 5.1 Patch 流程

```
1. GUI 验证 Output / Target 非空，Output 存在
2. 解析路径 → 识别本地/远程
3. 启动 PreCheckThread
   3a. 获取/验证 SSH 密码（如有远程路径）
   3b. check_patch_compatibility
   3c. find_overlapping_paths
4. 主线程收到 result：
   - NONE → 弹禁止对话框，终止
   - PARTIAL → ConfirmDialog 展示差异表格，等待用户确认
   - MATCH / EMPTY_TARGET → 直接通过
5. ConfirmDialog 展示重叠文件列表（最多 20 项），用户二次确认
6. 启动 WorkerThread
   6a. backup_overlapping_files
   6b. PatchOperation.run()
7. 成功 → 弹成功对话框
```

### 5.2 Rollback 流程

```
1. GUI 验证 Backup / Target 非空
2. 扫描 Backup Dir → 提取带时间戳的子目录 → 按时间倒序排列
3. 用户从下拉框选择备份版本
4. 同 Patch 流程 3-7（将 Output 替换为选中的备份目录）
```

---

## 6. 路径识别规则

| 格式 | 类型 | 处理 |
|------|------|------|
| `~` 或 `~/...` | 本地 | `os.path.expanduser()` |
| `user@host:/path` | 远程 | 解析为 (user, host, remote_path) |
| 其他绝对/相对路径 | 本地 | `os.path.abspath()` |

---

## 7. 错误处理策略

| 错误类型 | 处理方式 |
|----------|----------|
| **连接超时** (`ConnectionTimeoutError`) | 记录日志，GUI 弹对话框提示 "连接超时，请检查网络或重试" |
| **SFTP 读写超时** (`SFTPTimeoutError`) | 记录日志，GUI 弹对话框提示 "传输超时，请检查网络或重试" |
| 网络断开 / 连接重置 | 清理连接池中的失效连接，记录日志，GUI 提示重试 |
| 密码错误 | 清除缓存密码，弹出 PasswordDialog |
| 磁盘满 / 权限不足 | 记录日志，停止操作，弹错误对话框 |
| 目标被占用 | 记录日志，提示用户手动处理 |
| 操作被取消 | 清理临时文件/目录，记录日志，GUI 恢复按钮状态 |

---

## 8. 测试策略

- **本地操作**：pytest + `tempfile.TemporaryDirectory`，覆盖 patch / rollback / backup / 兼容性检查 / 重叠检测
- **远程操作**：mock `paramiko.SSHClient` / `SFTPClient`，验证 RemoteFS 和 SSHPool 的调用链
- **GUI**：不测界面布局，只测线程信号和数据流转

---

## 9. 技术栈

- Python 3
- **PySide6（首选）/ PySide2（降级兼容）**
- paramiko（SSH/SFTP）
- python-scp（大批量文件传输备选）
- JSON 配置

## 10. PySide6 / PySide2 兼容层

### 10.1 统一导入 (`gui/qt_compat.py`)

```python
try:
    from PySide6.QtCore import *
    from PySide6.QtGui import *
    from PySide6.QtWidgets import *
    PYSIDE_VERSION = 6
except ImportError:
    from PySide2.QtCore import *
    from PySide2.QtGui import *
    from PySide2.QtWidgets import *
    PYSIDE_VERSION = 2
```

### 10.2 已知差异处理

| 差异点 | PySide6 | PySide2 | 兼容方案 |
|--------|---------|---------|----------|
| `exec_()` | `exec()` | `exec_()` | 统一使用 `exec_()` 或检测后调用 |
| Signal 定义 | `@Signal(str)` | `@Signal(str)` | 语法一致，无需处理 |
| `QFileDialog.getExistingDirectory` | 参数 `options=QFileDialog.ShowDirsOnly` | 同 | 完全一致 |
| `QThread` | `QThread` | `QThread` | 完全一致 |
| 枚举值 | `Qt.WaitCursor` | `Qt.WaitCursor` | 完全一致 |
| `pyqtSignal` vs `Signal` | `PySide6.QtCore.Signal` | `PySide2.QtCore.Signal` | 统一从 qt_compat 导入 |

### 10.3 设计原则
- GUI 所有文件都从 `gui.qt_compat` 导入 Qt 类，不直接 `import PySideX`
- 运行时检测到 PySide6 则优先使用，否则降级到 PySide2
- 两个版本 API 差异极小，主要注意 `exec()` 在 Python 3 是关键字，PySide2 用 `exec_()`
