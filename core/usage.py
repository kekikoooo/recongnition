# -*- coding: utf-8 -*-
"""
Token 用量记账：每次模型调用的真实用量（输入 / 输出 / 思考）追加写入 output/<工作项>/usage_log.jsonl。

- 记到哪个工作项、哪道题，由当前线程的上下文决定（set_context），求解/检验的工作线程开始处理一道题时设置；
- 单题的用量另外累加到线程本地计数器（begin_problem / problem_usage），写进该题 slot 的 usage 字段；
- summarize 汇总整本书的用量（按阶段、按题），供网页显示。
"""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

_ctx = threading.local()
_LOCK = threading.Lock()
LOG_NAME = "usage_log.jsonl"

STAGE_NAMES = {
    "transcribe": "题干转写", "draft": "草稿推导", "review": "独立评审", "final": "定稿",
    "verify": "检验核对", "compact": "速查补生成", "crop_judge": "裁切判定", "crop": "裁切判定", "profile": "书籍嗅探",
    "tutor": "伴读问答", "crop_check": "AI 切题检查", "crop_fix": "AI 切题修复", "locate": "习题页定位",
}


def set_context(project_dir: Optional[Path] = None, pid: Optional[str] = None):
    _ctx.project = Path(project_dir) if project_dir else None
    _ctx.pid = str(pid) if pid is not None else None


def begin_problem():
    _ctx.acc = {"in": 0, "out": 0, "think": 0, "total": 0, "calls": 0}


def problem_usage() -> Dict[str, int]:
    return dict(getattr(_ctx, "acc", None) or {"in": 0, "out": 0, "think": 0, "total": 0, "calls": 0})


def record(stage: str, usage_metadata: Any, model: str = "", project_dir: Optional[Path] = None):
    """usage_metadata 为 google-genai 的 GenerateContentResponseUsageMetadata（可为 None）。"""
    if usage_metadata is None:
        return
    g = lambda k: int(getattr(usage_metadata, k, None) or 0)
    row = {"in": g("prompt_token_count"), "out": g("candidates_token_count"),
           "think": g("thoughts_token_count"), "total": g("total_token_count")}
    if not row["total"]:
        row["total"] = row["in"] + row["out"] + row["think"]
    acc = getattr(_ctx, "acc", None)
    if acc is not None:
        for k in ("in", "out", "think", "total"):
            acc[k] += row[k]
        acc["calls"] += 1
    proj = Path(project_dir) if project_dir else getattr(_ctx, "project", None)
    if proj is None or not proj.is_dir():
        return
    row.update({"t": round(time.time(), 1), "stage": stage, "pid": getattr(_ctx, "pid", None), "model": model})
    with _LOCK:
        with open(proj / LOG_NAME, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


_CACHE: Dict[str, Any] = {}


def summarize(project_dir: Path) -> Dict[str, Any]:
    """整本书的用量汇总；没有记录（旧项目）时 recorded=False。按文件修改时间缓存。"""
    p = Path(project_dir) / LOG_NAME
    if not p.exists():
        return {"recorded": False, "total": 0}
    key = str(p)
    mt = p.stat().st_mtime
    hit = _CACHE.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    tot = {"in": 0, "out": 0, "think": 0, "total": 0, "calls": 0}
    by_stage: Dict[str, Dict[str, int]] = {}
    pids = set()
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            st = by_stage.setdefault(r.get("stage") or "other", {"in": 0, "out": 0, "think": 0, "total": 0, "calls": 0})
            for d in (tot, st):
                for k in ("in", "out", "think", "total"):
                    d[k] += int(r.get(k) or 0)
                d["calls"] += 1
            if r.get("pid") and r.get("stage") in ("draft", "final"):
                pids.add(r["pid"])
    res = {"recorded": True, **tot, "solved_problems": len(pids),
           "per_problem": round(tot["total"] / len(pids)) if pids else 0,
           "by_stage": [{"stage": k, "name": STAGE_NAMES.get(k, k), **v}
                        for k, v in sorted(by_stage.items(), key=lambda kv: -kv[1]["total"])]}
    _CACHE[key] = (mt, res)
    return res
