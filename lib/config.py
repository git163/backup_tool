"""
配置读写模块
支持 JSON 格式的配置文件，包含 ssh_passwords 字段
"""

import json
import os
from typing import Any


class Config:
    def __init__(self):
        self.data: dict = {}

    def load(self, path: str) -> None:
        if not os.path.exists(path):
            self.data = {}
            return
        with open(path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    @property
    def ssh_passwords(self) -> dict[str, str]:
        return self.data.get('ssh_passwords', {})

    def set_ssh_password(self, user_host: str, password: str) -> None:
        if 'ssh_passwords' not in self.data:
            self.data['ssh_passwords'] = {}
        self.data['ssh_passwords'][user_host] = password

    def remove_ssh_password(self, user_host: str) -> None:
        passwords = self.data.get('ssh_passwords', {})
        if user_host in passwords:
            del passwords[user_host]
