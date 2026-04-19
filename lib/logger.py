"""
日志模块
- 文件 Handler：按天存储到 logs/YYYY-MM-DD.log
- Qt Signal Handler：供 GUI 实时显示
"""

import logging
import os
from datetime import datetime

from gui.qt_compat import QObject, Signal


class QtLogHandler(logging.Handler, QObject):
    """自定义 Handler，通过 Qt Signal 将日志发送到 GUI。"""
    log_signal = Signal(str)

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%H:%M:%S'
        ))

    def emit(self, record):
        msg = self.format(record)
        self.log_signal.emit(msg)


class AppLogger:
    """统一管理应用日志。"""

    @staticmethod
    def setup(name: str = "app", log_dir: str = "logs") -> logging.Logger:
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)

        if logger.handlers:
            return logger

        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(
            log_dir,
            datetime.now().strftime("%Y-%m-%d") + ".log"
        )

        # 文件 Handler
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(file_handler)

        # 控制台 Handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%H:%M:%S'
        ))
        logger.addHandler(console_handler)

        return logger
