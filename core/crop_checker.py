# -*- coding: utf-8 -*-
"""
AI 辅助检查切题：逐页把“原书这一页”和“从这一页切出来的各题切片”一起交给多模态模型，
按常见切题错误清单逐题判断，结果写入 output/<工作项>/crop_check_report.json，
网页的“切题检查”页和“待复核”筛选据此标出可疑切片。

每页一次调用（页图 + 本页各题切片，高分辨率），约 4~6 千 token / 页。
"""

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPORT = "crop_check_report.json"

ERROR_TYPES = [
    ("截断", "题目上下少了行，或左右切掉了字、公式、插图"),
    ("混入相邻题", "带进了上一题的末行、下一题的开头，或别题的零星符号（下标、求和号上限等）"),
    ("跨页漏拼", "题目在下一页还有内容，但切片里没有"),
    ("跨页误拼", "把下一页别的题或正文拼了进来"),
    ("插图错位", "本题的插图没在切片里，或混进了别题的插图"),
    ("混入非题目内容", "页眉、页脚、页码、章节标题、大题标题（如“三、计算题”）、脚注、大片答题空白"),
    ("并题", "一张切片里有两道题"),
    ("题号不符", "切片内容和给定题号对不上"),
]

CROPCHECK: Dict[str, Any] = {"is_running": False, "project": None, "done": 0, "total": 0, "message": "", "error": None}
_LOCK = threading.Lock()


def _prompt(pno: int, entries: List[Dict[str, Any]]) -> str:
    rules = "\n".join(f"{i + 1}. {n}：{d}" for i, (n, d) in enumerate(ERROR_TYPES))
    items = []
    for e in entries:
        note = ""
        if len(e["pages"]) > 1:
            note = f"（跨页题：切片由第 {'、'.join(map(str, e['pages']))} 页拼接而成，本页只是其中一部分）"
        items.append(f"- {e['pid']}{note}")
    return f"""第一张图是原书第 {pno} 页。后面依次是系统从这一页切出来的各道题的切片，题号如下：
{chr(10).join(items)}

请逐题对照原书页面，检查切片有没有下面这些常见错误：
{rules}

注意：试卷题目右侧自带的“得分”框是版面的一部分，不算错误；切片边缘少量留白不算错误；原书本身就有的空白、图注不算错误；跨页题只看本页这部分对不对，以及续页有没有接上。
另外，如果原书这一页上有题号却没有对应的切片，算“漏题”。

严格按下面格式输出，每题一行，不要输出其它内容：
题号 | OK
题号 | 错误类型 | 一句话说明（具体指出缺了什么/多了什么）
漏题 | 题号 | 一句话说明"""


_LINE = re.compile(r"^\s*(?:[-*]\s*)?(\d{1,2}\.\d{1,3})\s*[|｜]\s*([^|｜]+?)\s*(?:[|｜]\s*(.*))?$")
_MISS = re.compile(r"^\s*(?:[-*]\s*)?漏题\s*[|｜]\s*([^|｜]+?)\s*(?:[|｜]\s*(.*))?$")


def _parse(text: str, pids: List[str]) -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    missing = []
    for ln in (text or "").splitlines():
        m = _MISS.match(ln)
        if m:
            missing.append({"pid": m.group(1).strip(), "detail": (m.group(2) or "").strip()})
            continue
        m = _LINE.match(ln)
        if not m or m.group(1) not in pids:
            continue
        kind = m.group(2).strip()
        if kind.upper() == "OK":
            res.setdefault(m.group(1), {"ok": True, "problems": []})
        else:
            r = res.setdefault(m.group(1), {"ok": False, "problems": []})
            r["ok"] = False
            r["problems"].append({"type": kind, "detail": (m.group(3) or "").strip()})
    return {"results": res, "missing": missing}


def _pages_index(p_dir: Path) -> Dict[int, List[Dict[str, Any]]]:
    """页码 -> 这一页上切出来的题（来自 problems.json 里记录的切割框）"""
    by_page: Dict[int, List[Dict[str, Any]]] = {}
    for pj in sorted(p_dir.glob("Chapter_*/problems.json")):
        for prob in json.loads(pj.read_text(encoding="utf-8")):
            boxes = prob.get("boxes") or []
            pages = sorted({b[0] for b in boxes})
            sp = pj.parent / "pages" / f"problem_{prob['problem_id']}_slice.png"
            for pg in pages:
                img = pj.parent / "pages" / f"page_{pg}.png"
                by_page.setdefault(pg, []).append({"pid": str(prob["problem_id"]), "slice": sp, "page_img": img,
                                                    "pages": pages})
    return dict(sorted(by_page.items()))


def estimate(p_dir: Path) -> Dict[str, Any]:
    idx = _pages_index(Path(p_dir))
    n_slices = sum(len(v) for v in idx.values())
    # 高分辨率每张图约 1100 token，外加提示词、思考与输出约 1500 / 页
    tokens = len(idx) * (1100 + 1500) + n_slices * 1100
    return {"pages": len(idx), "problems": len({e["pid"] for v in idx.values() for e in v}),
            "slices": n_slices, "tokens": tokens, "has_boxes": bool(idx)}


def run_check(p_dir: Path, concurrency: int = 3, only_pages: Optional[List[int]] = None):
    """only_pages：只复查这些页（修复后），结果并入原报告"""
    from google.genai import types
    from core import usage
    from core.config import load_book_config
    from core.solver import init_gemini_client, _call, _model_name
    p_dir = Path(p_dir)
    cfg = load_book_config(p_dir)
    model = _model_name(cfg)
    client = init_gemini_client(cfg)
    idx = _pages_index(p_dir)
    if only_pages:
        idx = {k: v for k, v in idx.items() if k in set(only_pages)}
    CROPCHECK.update(is_running=True, project=p_dir.name, done=0, total=len(idx), message="开始逐页检查…", error=None)
    results: Dict[str, Any] = {}
    missing: List[Dict[str, Any]] = []

    def one(pg: int, entries: List[Dict[str, Any]]):
        usage.set_context(p_dir, None)
        if not entries[0]["page_img"].exists():
            return pg, {"results": {}, "missing": []}
        parts = [_prompt(pg, entries), types.Part.from_bytes(data=entries[0]["page_img"].read_bytes(), mime_type="image/png")]
        for e in entries:
            if e["slice"].exists():
                parts += [f"【切片 {e['pid']}】", types.Part.from_bytes(data=e["slice"].read_bytes(), mime_type="image/png")]
        out = _call(client, model, "你是严谨的教材排版质检员，只做核对，不解题。", parts, 0.0, tries=4, stage="crop_check")
        return pg, _parse(out, [e["pid"] for e in entries])

    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(one, pg, es) for pg, es in idx.items()]
            for f in futs:
                try:
                    pg, r = f.result()
                except Exception as e:
                    CROPCHECK["message"] = f"有一页检查失败：{e}"
                    CROPCHECK["done"] += 1
                    continue
                for pid, v in r["results"].items():
                    cur = results.setdefault(pid, {"ok": True, "problems": [], "pages": []})
                    cur["pages"].append(pg)
                    if not v["ok"]:
                        cur["ok"] = False
                        cur["problems"] += [dict(x, page=pg) for x in v["problems"]]
                missing += [dict(m, page=pg) for m in r["missing"]]
                CROPCHECK["done"] += 1
                CROPCHECK["message"] = f"已检查 {CROPCHECK['done']}/{CROPCHECK['total']} 页"
        if only_pages and (p_dir / REPORT).exists():
            old = json.loads((p_dir / REPORT).read_text(encoding="utf-8"))
            merged = dict(old.get("results", {}))
            merged.update(results)
            missing = [m for m in old.get("missing", []) if m.get("page") not in set(only_pages)] + missing
            results = merged
        bad = sorted(pid for pid, v in results.items() if not v["ok"])
        report = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "pages": len(_pages_index(p_dir)),
                  "checked_problems": len(results), "flagged": bad, "results": results, "missing": missing,
                  "rules": [n for n, _ in ERROR_TYPES]}
        (p_dir / REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        CROPCHECK["message"] = (f"检查完成：{len(results)} 题中标出 {len(bad)} 题可疑" +
                                (f"，另有 {len(missing)} 处疑似漏题" if missing else ""))
        CROPCHECK["flagged"] = len(bad)
        CROPCHECK["missing"] = len(missing)
    except Exception as e:
        CROPCHECK.update(error=str(e), message=f"检查失败：{e}")
    finally:
        CROPCHECK["is_running"] = False


def start_check(p_dir: Path) -> bool:
    with _LOCK:
        if CROPCHECK["is_running"]:
            return False
        CROPCHECK["is_running"] = True
    threading.Thread(target=run_check, args=(Path(p_dir),), daemon=True).start()
    return True


def issues_for(p_dir: Path) -> Dict[str, List[str]]:
    """题号 -> 可读的问题描述（供待复核与切题检查页显示）"""
    rep = Path(p_dir) / REPORT
    if not rep.exists():
        return {}
    try:
        r = json.loads(rep.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, List[str]] = {}
    for pid, v in r.get("results", {}).items():
        if not v.get("ok"):
            out[pid] = [f"AI 切题检查：{x['type']}" + (f"（{x['detail']}）" if x.get("detail") else "") for x in v["problems"]]
    return out
