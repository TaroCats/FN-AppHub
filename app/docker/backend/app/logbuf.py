"""运行日志：同时写入文件 + 内存环形缓冲，供前端「调试」面板读取。"""
from __future__ import annotations

import logging
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

MAX_ITEMS = 2000


class RingHandler(logging.Handler):
    """把日志写进内存队列，前端轮询展示。"""

    def __init__(self, capacity: int = MAX_ITEMS) -> None:
        super().__init__()
        self.items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.seq = 0

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.seq += 1
            item = {
                "seq": self.seq,
                "ts": round(record.created, 3),
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                item["msg"] += " | " + self.format(record).splitlines()[-1][:400]
            self.items.append(item)
        except Exception:  # 日志自身绝不能再抛错
            pass

    def snapshot(self, level: str = "", limit: int = 200, since: int = 0, keyword: str = "") -> list[dict[str, Any]]:
        levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        threshold = levels.get((level or "").upper(), 0)
        out: list[dict[str, Any]] = []
        for item in self.items:
            if item["seq"] <= since:
                continue
            if threshold and levels.get(item["level"], 0) < threshold:
                continue
            if keyword and keyword.lower() not in item["msg"].lower():
                continue
            out.append(item)
        return out[-max(1, min(limit, 1000)):]

    def clear(self) -> None:
        self.items.clear()


RING = RingHandler()
_FILE_HANDLER: Optional[logging.Handler] = None
_CONFIGURED = False


def setup(data_dir: Path, level: str = "INFO") -> None:
    """初始化日志：控制台 + 滚动文件 + 内存环。"""
    global _FILE_HANDLER, _CONFIGURED
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not _CONFIGURED:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)
        try:
            log_dir = data_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            _FILE_HANDLER = RotatingFileHandler(
                log_dir / "apphub.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            _FILE_HANDLER.setFormatter(fmt)
            root.addHandler(_FILE_HANDLER)
        except OSError:
            pass
        RING.setFormatter(fmt)
        root.addHandler(RING)
        for noisy in ("httpx", "httpcore", "urllib3", "watchfiles"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _CONFIGURED = True
    set_level(level)


def set_level(level: str) -> None:
    lv = getattr(logging, (level or "INFO").upper(), logging.INFO)
    logging.getLogger().setLevel(lv)
    logging.getLogger("apphub").setLevel(lv)
    logging.getLogger("apphub.proxy").setLevel(lv)


def log_file_path(data_dir: Path) -> str:
    return str(data_dir / "logs" / "apphub.log")
