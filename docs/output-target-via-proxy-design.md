# Output 远程时 Target 跳板访问适配 — 详细设计（方案 B）

## 1. 背景与目标

### 1.1 问题描述
- `output` 为可直接 SSH 访问的远程主机（gateway）
- `target` 为 gateway 内网中的机器（"小网"），本地无法直接 SSH，只能通过 gateway 作为跳板
- 小网机器**不能主动访问任何外部机器**（仅接受被动连接）
- 当前代码不支持 SSH ProxyJump，该场景完全不生效

### 1.2 设计目标
1. Target 旁增加 "Via Output Host" 复选框，勾选后 target 通过 output 主机作为跳板访问
2. 补丁、备份、回滚、浏览目录、预检等所有功能在跳板模式下正常工作
3. Remote→Remote 文件复制采用 SFTP 流式中转（不落地本地磁盘）
4. 所有连接均由本地主动发起，target 不主动访问任何机器

---

## 2. 总体架构

### 2.1 网络拓扑与数据流

```
┌─────────────┐
│  本地工具    │
└──────┬──────┘
       │ 1. SSH 直连
       ▼
┌─────────────────┐
│ gateway (output) │  ← user@gateway_host:/data/output
│  可被本地直连     │
└──────┬──────────┘
       │ 2. SSH direct-tcpip 通道转发（由本地发起）
       ▼
┌─────────────────┐
│ internal(target)│  ← user@internal_host:/data/target
│  仅 gateway 可达 │
│  不主动连任何机器 │
└─────────────────┘
```

### 2.2 文件复制数据流（SFTP 流式）

**场景：Patch（output → target）**
```
本地 ──SSH A──→ gateway ──SSH B──→ target
     │                    │
     └─ sftpA.open(rb) ──┘
            │
            ▼
     分块读取 (32KB chunks)
            │
            ▼
     sftpB.putfo() → 写入 target
```

**场景：Backup（target → 本地 backup）**
```
本地 ──SSH A──→ gateway ──SSH B──→ target
     │                    │
     └─ sftpB.open(rb) ──┘
            │
            ▼
     分块读取 (32KB chunks)
            │
            ▼
     本地文件系统写入
```

**内存占用**：始终只有 32KB 缓冲，不依赖文件大小。

---

## 3. 模块详细设计

### 3.1 `lib/ssh_client.py` — SSH 跳板连接

#### 3.1.1 `SSHConnection` 改造

```python
class SSHConnection:
    """封装 paramiko.SSHClient + SFTPClient，支持复用。"""

    def __init__(self, host: str, user: str, password: str, sock=None):
        # 新增 sock 参数，用于跳板连接
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
                sock=sock,  # ← 新增
            )
            # ... 现有 SFTP 初始化逻辑
```

#### 3.1.2 新增 `open_proxy_connection`

```python
def open_proxy_connection(self, target_host: str, target_user: str, target_password: str) -> "SSHConnection":
    """
    通过当前 SSH 连接建立到 target 的跳板连接。
    使用 SSH direct-tcpip 通道转发，gateway 上无需执行任何远程命令。
    """
    transport = self.client.get_transport()
    if transport is None or not transport.is_active():
        raise ConnectionTimeoutError("SSH connection lost")

    # 在 gateway 上打开到 target:22 的 TCP 转发通道
    channel = transport.open_channel(
        "direct-tcpip",
        (target_host, 22),
        ("127.0.0.1", 0),
    )
    if channel is None:
        raise ConnectionTimeoutError(f"Failed to open proxy channel to {target_host}:22")

    return SSHConnection(target_host, target_user, target_password, sock=channel)
```

**说明**：
- `direct-tcpip` 是 SSH 协议内置的端口转发机制，gateway 仅做 TCP 层转发
- 不需要在 gateway 上安装额外工具或执行命令
- 所有认证仍由本地 paramiko 完成

---

### 3.2 `gui/main_window.py` — UI 与配置

#### 3.2.1 UI 改动

**Target 行增加复选框**：

```python
# 现有代码
grid.addWidget(QLabel("Target Dir:"), 2, 0)
grid.addWidget(self.target_edit, 2, 1)
grid.addWidget(self.target_btn, 2, 2)

# 改为：
grid.addWidget(QLabel("Target Dir:"), 2, 0)
grid.addWidget(self.target_edit, 2, 1)
target_btn_layout = QHBoxLayout()
target_btn_layout.addWidget(self.target_btn)
self.target_via_checkbox = QCheckBox("Via Output Host")
target_btn_layout.addWidget(self.target_via_checkbox)
grid.addLayout(target_btn_layout, 2, 2)
```

#### 3.2.2 配置加载/保存

```python
def _load_defaults(self):
    self.backup_edit.setText(self.config.get("backup", ""))
    self.output_edit.setText(self.config.get("output", ""))
    self.target_edit.setText(self.config.get("target", ""))
    self.target_via_checkbox.setChecked(self.config.get("target_via_output", False))  # ← 新增

def _save_config(self):
    self.config.set("backup", self.backup_edit.text().strip())
    self.config.set("output", self.output_edit.text().strip())
    self.config.set("target", self.target_edit.text().strip())
    self.config.set("target_via_output", self.target_via_checkbox.isChecked())  # ← 新增
    self.config.save(self.config_path)
```

**配置 JSON 格式示例**：
```json
{
  "backup": "~/backup_tool/backup",
  "output": "user@gateway_host:/data/output",
  "target": "user@internal_host:/data/target",
  "target_via_output": true,
  "ssh_passwords": {
    "user@gateway_host": "password1",
    "user@internal_host": "password2"
  }
}
```

#### 3.2.3 Browse 逻辑改造

```python
def _browse(self, edit: QLineEdit):
    path = edit.text().strip()
    is_remote, user, host, real_path = parse_path(path)

    if edit == self.target_edit and self.target_via_checkbox.isChecked():
        # 通过 output 主机作为跳板浏览 target
        self._browse_target_via_output(path)
        return

    # 保持现有逻辑（本地或直连远程）
    # ... 现有代码

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

    # 获取 output 主机密码
    out_password = self.config.ssh_passwords.get(out_user_host)
    if not out_password:
        out_password = self._ask_password(out_user_host)
        if not out_password:
            return

    # 获取 target 密码
    tgt_password = self.config.ssh_passwords.get(tgt_user_host)
    if not tgt_password:
        tgt_password = self._ask_password(tgt_user_host)
        if not tgt_password:
            return

    # 建立 output 连接，然后通过它建立到 target 的跳板连接
    proxy_conn = self.ssh_pool.get(out_user_host, out_password)
    try:
        target_conn = proxy_conn.open_proxy_connection(tgt_host, tgt_user, tgt_password)
    except Exception as e:
        QMessageBox.warning(self, "Connection Failed", f"Failed to connect to target via output host: {e}")
        return

    # 使用跳板连接打开远程目录浏览器
    dialog = RemoteDirDialog(
        self.ssh_pool, tgt_user_host, tgt_password,
        tgt_real or "/", self, proxy_conn=target_conn
    )
    if dialog.exec_() == QDialog.Accepted:
        selected = f"{tgt_user_host}:{dialog.selected_path}"
        self.target_edit.setText(selected)
```

---

### 3.3 `gui/dialogs.py` — 远程目录浏览器支持跳板

#### 3.3.1 `_RemoteDirLoader` 改造

```python
class _RemoteDirLoader(QThread):
    loaded = Signal(list)
    error = Signal(str)

    def __init__(self, ssh_pool: SSHPool, user_host: str, password: str, path: str, proxy_conn=None):
        super().__init__()
        self.ssh_pool = ssh_pool
        self.user_host = user_host
        self.password = password
        self.path = path
        self.proxy_conn = proxy_conn  # ← 新增

    def run(self):
        try:
            if self.proxy_conn:
                # 使用已有的跳板连接（不再通过 ssh_pool）
                fs = RemoteFS(self.proxy_conn)
            else:
                conn = self.ssh_pool.get(self.user_host, self.password)
                fs = RemoteFS(conn)
            # ... 现有遍历逻辑
```

#### 3.3.2 `RemoteDirDialog` 改造

```python
class RemoteDirDialog(QDialog):
    def __init__(self, ssh_pool: SSHPool, user_host: str, password: str,
                 initial_path: str = "/", parent=None, proxy_conn=None):
        super().__init__(parent)
        self.ssh_pool = ssh_pool
        self.user_host = user_host
        self.password = password
        self.proxy_conn = proxy_conn  # ← 新增
        # ...

    def _load_dir(self, path: str):
        # ...
        self.loader = _RemoteDirLoader(
            self.ssh_pool, self.user_host, self.password, path,
            proxy_conn=self.proxy_conn  # ← 新增
        )
        # ...
```

---

### 3.4 `gui/thread.py` — 工作线程支持跳板

#### 3.4.1 线程初始化改造

```python
class PreCheckThread(QThread):
    def __init__(self, output_path: str, target_path: str, ssh_pool: SSHPool, config,
                 target_via_output: bool = False):  # ← 新增
        super().__init__()
        self.output_path = output_path
        self.target_path = target_path
        self.ssh_pool = ssh_pool
        self.config = config
        self.target_via_output = target_via_output  # ← 新增

    def run(self):
        try:
            output_fs, output_real = self._get_fs(self.output_path)
            target_fs, target_real = self._get_target_fs()  # ← 使用新方法
            # ... 现有逻辑

class WorkerThread(QThread):
    def __init__(self, operation_type: str, paths: dict, ssh_pool: SSHPool, config,
                 target_via_output: bool = False):  # ← 新增
        super().__init__()
        self.operation_type = operation_type
        self.paths = paths
        self.ssh_pool = ssh_pool
        self.config = config
        self.target_via_output = target_via_output  # ← 新增
```

#### 3.4.2 新增 `_get_target_fs`

```python
def _get_target_fs(self):
    """获取 target 的 FileSystem，支持通过 output 跳板。"""
    if not self.target_via_output:
        return self._get_fs(self.target_path)

    # 解析 output 作为跳板
    out_is_remote, out_user, out_host, out_real = parse_path(self.output_path)
    if not out_is_remote:
        raise ValueError("Output must be a remote path when using Via Output Host")

    # 解析 target
    tgt_is_remote, tgt_user, tgt_host, tgt_real = parse_path(self.target_path)
    if not tgt_is_remote:
        raise ValueError("Target must be a remote path when using Via Output Host")

    # 获取 output 连接作为跳板
    out_user_host = f"{out_user}@{out_host}"
    out_password = self.config.ssh_passwords.get(out_user_host)
    if not out_password:
        raise AuthenticationError(f"AUTH:{out_user_host}")
    proxy_conn = self.ssh_pool.get(out_user_host, out_password)

    # 获取 target 密码
    tgt_user_host = f"{tgt_user}@{tgt_host}"
    tgt_password = self.config.ssh_passwords.get(tgt_user_host)
    if not tgt_password:
        raise AuthenticationError(f"AUTH:{tgt_user_host}")

    # 通过跳板建立 target 连接
    target_conn = proxy_conn.open_proxy_connection(tgt_host, tgt_user, tgt_password)
    return RemoteFS(target_conn), tgt_real
```

#### 3.4.3 各操作方法改造

所有涉及 target 的操作统一使用 `_get_target_fs()`：

```python
def _backup_overlapping(self, require_backup: bool = False):
    output_path = self.paths['output']
    target_path = self.paths['target']
    backup_dir = self.paths.get('backup', '')

    output_fs, output_real = self._get_fs(output_path)
    target_fs, target_real = self._get_target_fs()  # ← 修改
    # ...

def _do_patch(self):
    self._backup_overlapping()
    if self._is_cancelled():
        return

    output_fs, output_real = self._get_fs(self.paths['output'])
    target_fs, target_real = self._get_target_fs()  # ← 修改
    # ...

def _do_rollback(self):
    backup_fs, backup_real = self._get_fs(self.paths['backup'])
    target_fs, target_real = self._get_target_fs()  # ← 修改
    # ...

def _do_backup(self):
    target_fs, target_real = self._get_target_fs()  # ← 修改
    backup_fs, backup_real = self._get_fs(self.paths['backup'])
    # ...
```

#### 3.4.4 `PreCheckThread.run` 改造

```python
def run(self):
    try:
        output_fs, output_real = self._get_fs(self.output_path)
        target_fs, target_real = self._get_target_fs()  # ← 修改
        # ... 现有逻辑
```

**注意**：`PreCheckThread` 也需要新增 `_get_target_fs` 方法（与 `WorkerThread` 相同）。

---

### 3.5 `lib/operations.py` — 主复制逻辑改为 SFTP 流式

#### 3.5.1 `PatchOperation._copy_file` 改造

```python
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
```

#### 3.5.2 `RemoteFS` 需要暴露 `sftp`

`RemoteFS` 已有 `self.sftp = ssh_conn.sftp`，可直接访问。无需额外改动。

---

### 3.6 `lib/compat.py` — 备份重叠文件改为 SFTP 流式

#### 3.6.1 `_copy_between_fs` 改造

```python
def _copy_between_fs(
    src_fs: FileSystem,
    src_path: str,
    dst_fs: FileSystem,
    dst_path: str,
    logger,
    cancelled_callback=None,
) -> None:
    if cancelled_callback and cancelled_callback():
        raise RuntimeError("Operation cancelled")

    if src_fs.isfile(src_path):
        if isinstance(src_fs, RemoteFS) and isinstance(dst_fs, RemoteFS):
            # Remote -> Remote (SFTP 流式中转)
            src_file = src_fs.sftp.open(src_path, 'rb')
            try:
                dst_fs.sftp.putfo(src_file, dst_path)
            finally:
                src_file.close()
        elif isinstance(src_fs, RemoteFS):
            src_fs.download_file(src_path, dst_path)
        elif isinstance(dst_fs, RemoteFS):
            dst_fs.upload_file(src_path, dst_path)
        else:
            dst_fs.copy(src_path, dst_path)
    elif src_fs.isdir(src_path):
        dst_fs.makedirs(dst_path, exist_ok=True)
        for name in src_fs.listdir(src_path):
            _copy_between_fs(
                src_fs, src_fs.join(src_path, name),
                dst_fs, dst_fs.join(dst_path, name),
                logger, cancelled_callback
            )
```

---

## 4. 错误处理

### 4.1 认证错误
- **output 主机密码错误**：`AuthenticationError` 抛出，GUI 捕获后弹窗要求重新输入
- **target 主机密码错误**：同样通过 `AuthenticationError` 处理
- **key 区分**：`user@gateway_host` 和 `user@internal_host` 分别作为独立 key 存储在 `Config.ssh_passwords` 中

### 4.2 连接错误
- **output 主机不可达**：`ConnectionTimeoutError`，GUI 弹窗提示
- **target 通过跳板不可达**：`ConnectionTimeoutError`（由 `open_proxy_channel` 失败触发），GUI 弹窗提示
- **SSH 通道打开失败**：`direct-tcpip` 通道返回 None 时，抛出 `ConnectionTimeoutError`

### 4.3 配置错误
- **勾选 Via Output Host 但 output 为本地路径**：`_get_target_fs` 中抛出 `ValueError`，GUI 捕获后弹窗提示
- **勾选 Via Output Host 但 target 为本地路径**：同上

---

## 5. 边界约束与 UI 禁止逻辑

### 5.1 核心前置约束

| 约束 | 说明 | 是否 UI 禁止 |
|------|------|-------------|
| **output 必须为远程路径** | 跳板模式需要一台可直连的 SSH 主机作为 gateway | ✅ 是 |
| **target 必须为远程路径** | 跳板模式用于访问远程内网机器，本地路径无需跳板 | ✅ 是 |
| **远程路径不支持自定义端口** | 当前 `parse_path` 和 `SSHConnection` 均不支持 `user@host:port:/path` 格式，host 会被解析为 `gateway:port`，导致 paramiko 连接失败 | ⚠️ 是（校验时一并拒绝非标准格式） |
| **SSH 端口固定为 22** | `open_proxy_connection` 硬编码 `target_host:22` | — |

### 5.2 UI 禁止/校验逻辑

所有涉及 target 的操作入口统一校验，避免用户到后台线程才报错：

**校验函数 `_validate_via_output()`**：

```python
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
```

**校验调用点**：

| 入口 | 校验时机 |
|------|---------|
| **Browse（Target）** | 点击 `target_btn` 时，若 checkbox 已勾选，先调用 `_validate_via_output()` |
| **Patch** | `_validate_patch_input()` 之后追加 `_validate_via_output()` |
| **Backup** | `_validate_patch_input(require_backup=True)` 之后追加 `_validate_via_output()` |
| **Rollback** | 在检查 backup/target 非空之后追加 `_validate_via_output()` |

### 5.3 其他逻辑边界

| 场景 | 行为 | 是否需要禁止 |
|------|------|-------------|
| **output 和 target 是同一台远程主机** | 逻辑可行（会建立到自身的跳板连接），但无意义。代码正常处理，不禁止。 | ❌ 否 |
| **backup 为远程路径（非 output 主机）** | Patch 时的重叠备份：`target(via proxy) → backup(remote)` 走 SFTP 流式，正常工作。 | ❌ 否 |
| **backup 为本地路径** | `target(via proxy) → local` 走 `download_file`，正常工作。 | ❌ 否 |
| **回滚时 backup 为远程** | `backup(remote) → target(via proxy)` 走 SFTP 流式，正常工作。 | ❌ 否 |
| **未勾选 Via Output Host** | 保持原有全部逻辑，checkbox 状态不影响任何已有功能。 | ❌ 否 |

### 5.4 连接阶段错误

| 错误场景 | 错误类型 | UI 表现 |
|---------|---------|---------|
| output 主机密码错误 | `AuthenticationError` | 弹窗要求重新输入密码 |
| target 主机密码错误 | `AuthenticationError` | 弹窗要求重新输入密码 |
| output 主机不可达 | `ConnectionTimeoutError` | `QMessageBox.critical` 提示连接失败 |
| target 通过跳板不可达 | `ConnectionTimeoutError` | 同上，提示 "Failed to connect to target via output host" |

---

## 6. 验证步骤

### 5.1 环境准备
1. 准备一台可直连的 gateway 主机（output）
2. 在 gateway 内网中准备一台 target 主机，仅 gateway 可 SSH 访问
3. 确保 target 不能主动 SSH 到任何外部机器

### 5.2 功能验证

| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 配置 output 为 `user@gateway:/data/output`，target 为 `user@internal:/data/target`，勾选 Via Output Host | 配置正常保存，JSON 中 `target_via_output: true` |
| 2 | 点击 Target 的 Browse | 弹出远程目录浏览器，显示 internal 主机的目录结构 |
| 3 | 点击 Patch（output 和 target 有重叠文件） | 预检通过，备份重叠文件，补丁成功应用到 target |
| 4 | 点击 Backup | target 完整备份到 backup 目录 |
| 5 | 选择备份版本，点击 Rollback | 备份成功恢复到 target |
| 6 | 取消勾选 Via Output Host，target 改为本地路径 | 所有功能正常工作，不受跳板逻辑干扰 |
| 7 | 大文件补丁（>100MB） | 内存占用稳定（约 32KB 缓冲），不落地本地磁盘 |

### 5.3 网络约束验证
- 在 target 上执行 `iptables -A OUTPUT -p tcp --dport 22 -j DROP`（禁止出站 SSH）
- 重复步骤 3-5，确认功能仍正常（因为所有连接都是本地主动发起，target 作为被动服务端）

---

## 6. 改动文件清单

| 文件 | 改动类型 | 改动内容 |
|------|---------|---------|
| `lib/ssh_client.py` | 修改 + 新增 | `SSHConnection.__init__` 增加 `sock` 参数；新增 `open_proxy_connection` |
| `gui/main_window.py` | 修改 | Target 行增加 `QCheckBox("Via Output Host")`；配置加载/保存；Browse 逻辑改造；线程初始化传入 `target_via_output` |
| `gui/dialogs.py` | 修改 | `_RemoteDirLoader` 和 `RemoteDirDialog` 支持 `proxy_conn` |
| `gui/thread.py` | 修改 | `PreCheckThread` 和 `WorkerThread` 新增 `target_via_output` 参数；新增 `_get_target_fs`；各操作使用新方法 |
| `lib/operations.py` | 修改 | `PatchOperation._copy_file` Remote→Remote 改为 SFTP 流式 |
| `lib/compat.py` | 修改 | `_copy_between_fs` Remote→Remote 改为 SFTP 流式 |
