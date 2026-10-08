# -*- coding: utf-8 -*-
"""
过程事件日志：整个构建过程“哪个时间在干什么”追加写入 output/<工作项>/events.log，
供后端 PowerShell 监控面板（monitor.ps1）读取。每行：时间 | 类型 | 内容。
类型：阶段 / 请求 / 返回 / 失败 / 题目 / 提示
"""

import threading
import time
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()
_throttle = {}
LOG_NAME = "events.log"


def _dir() -> Optional[Path]:
    try:
        from core import usage
        p = getattr(usage._ctx, "project", None)
        if p:
            return Path(p)
        from core.ingest_job import INGEST
        from core.config import DATA_ROOT
        name = INGEST.get("project")
        return Path(DATA_ROOT) / name if name else None
    except Exception:
        return None


def log(kind: str, msg: str, throttle: float = 0, key: str = "", project_dir: Optional[Path] = None):
    """追加一行；throttle>0 时同一 key 在这么多秒内只写一次（进度类消息防刷屏）"""
    try:
        d = Path(project_dir) if project_dir else _dir()
        if d is None or not d.is_dir():
            return
        now = time.time()
        if throttle:
            k = (str(d), key or kind)
            if now - _throttle.get(k, 0) < throttle:
                return
            _throttle[k] = now
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))} | {kind} | {msg}\n"
        with _LOCK:
            with open(d / LOG_NAME, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass
