# -*- coding: utf-8 -*-
"""
AI 一键修复切题：针对 AI 切题检查标出的题，让模型给出“怎么改”，由代码落到具体坐标。

模型不报坐标（不准），只说操作，引用原书里某一行开头的文字作为锚点：
  下边到 | 题号 | 应保留的最后一行开头文字      -> 下边移到这一行的下沿
  上边到 | 题号 | 应保留的第一行开头文字        -> 上边移到这一行的上沿
  续下一页 | 题号                              -> 加上下一页开头到下一题之前的区域
  去掉续页 | 题号                              -> 去掉拼在后面的其它页区域
  需要手动 | 题号 | 原因                        -> 版式太复杂（插图并排、图文混排），留给人工在切题检查页调整
锚点文字用本地 OCR 识别出的行（含坐标）做模糊匹配，找不到就记为需要手动。
修复后按人工框保存（crop_overrides.json），并对涉及的页重新做一次 AI 检查。
"""

import json
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

CROPFIX: Dict[str, Any] = {"is_running": False, "project": None, "done": 0, "total": 0, "message": "", "error": None}
_LOCK = threading.Lock()


def _ocr_lines(p_dir: Path, page: int, dpi: int = 150) -> List[Dict[str, Any]]:
    f = p_dir / "_cache" / f"ocr_dpi{dpi}" / f"p{page - 1:04d}.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("lines", [])
    except Exception:
        return []


def _norm(s: str) -> str:
    return re.sub(r"[\s$\\{}^_、，。,.:：；;（）()\[\]【】]", "", s or "")


def _find_line(lines: List[Dict[str, Any]], quote: str, y_lo: float, y_hi: float) -> Optional[Dict[str, Any]]:
    """在 [y_lo, y_hi] 范围内找开头文字最像 quote 的 OCR 行"""
    q = _norm(quote)[:12]
    if len(q) < 2:
        return None
    best, score = None, 0.0
    for ln in lines:
        yc = (ln["y0"] + ln["y1"]) / 2
        if not (y_lo <= yc <= y_hi):
            continue
        t = _norm(ln.get("text", ""))[:len(q) + 2]
        if not t:
            continue
        sc = SequenceMatcher(None, q, t).ratio()
        if q[:4] and t.startswith(q[:4]):
            sc += 0.3
        if sc > score:
            best, score = ln, sc
    return best if score >= 0.55 else None


def _page_img(p_dir: Path, page: int) -> Optional[Path]:
    for ch_dir in sorted(p_dir.glob("Chapter_*")):
        f = ch_dir / "pages" / f"page_{page}.png"
        if f.exists():
            return f
    return None


def _overlay(p_dir: Path, page: int, all_boxes: Dict[str, List[List[int]]], focus: str) -> Optional[bytes]:
    """原页 + 各题切割框（当前题红色，其余蓝色），给模型看现在是怎么切的"""
    f = _page_img(p_dir, page)
    if f is None:
        return None
    im = Image.open(f).convert("RGB")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("msyh.ttc", 22)
    except Exception:
        font = ImageFont.load_default()
    for pid, boxes in all_boxes.items():
        for b in boxes:
            if b[0] != page:
                continue
            col = (220, 20, 60) if pid == focus else (79, 70, 229)
            d.rectangle([b[3], b[1], b[4], b[2]], outline=col, width=4 if pid == focus else 2)
            d.text((b[3] + 4, b[1] + 2), pid, fill=col, font=font)
    import io
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


PROMPT = """你在修正教材题目的自动切图。图中是原书页面，框表示每道题目前切下来的区域（红框是要修的题 {pid}，蓝框是其它题）。
最后一张图是 {pid} 现在切出来的图片。AI 检查认为它有这些问题：
{issues}

请给出修正操作，只能用下面几种，每行一个；引用的文字写原书那一行**开头的 6~12 个字**（照原书抄，公式可以略写）：
下边到 | {pid} | 应保留的最后一行开头文字
上边到 | {pid} | 应保留的第一行开头文字
续下一页 | {pid}
去掉续页 | {pid}
需要手动 | {pid} | 原因（插图和别的题并排、图文左右混排等无法用上下边界解决的情况）
如果切图其实没有问题，输出：没问题 | {pid}
只输出操作行。"""


def _plan_ops(text: str, pid: str) -> List[Dict[str, str]]:
    ops = []
    for ln in (text or "").splitlines():
        parts = [x.strip() for x in re.split(r"[|｜]", ln)]
        if len(parts) >= 2 and parts[1] == pid and parts[0] in ("下边到", "上边到", "续下一页", "去掉续页", "需要手动", "没问题"):
            ops.append({"op": parts[0], "arg": parts[2] if len(parts) > 2 else ""})
    return ops


def _apply(p_dir: Path, pid: str, boxes: List[List[int]], ops: List[Dict[str, str]], all_boxes, order: List[str]):
    """把操作落到坐标上；返回 (新框, 说明列表, 是否需要手动)"""
    boxes = [b[:] for b in boxes]
    notes, manual = [], False
    first, last = boxes[0], boxes[-1]
    for o in ops:
        if o["op"] == "没问题":
            notes.append("模型认为切图没有问题")
        elif o["op"] == "需要手动":
            manual = True
            notes.append("需要手动：" + o["arg"])
        elif o["op"] in ("下边到", "上边到"):
            target = last if o["op"] == "下边到" else first
            lines = _ocr_lines(p_dir, target[0])
            # 在当前框附近（上下各放宽一大段）找锚点行
            ln = _find_line(lines, o["arg"], target[1] - 400, target[2] + 400)
            if ln is None:
                manual = True
                notes.append(f"没找到“{o['arg']}”这一行，需要手动")
                continue
            if o["op"] == "下边到":
                target[2] = int(ln["y1"]) + 8
                # 公式的下标/下限可能比 OCR 行框低一点：往下吃到下一段空白
                notes.append(f"下边移到“{o['arg']}”之后")
            else:
                new_top = max(0, int(ln["y0"]) - 8)
                # 第一块的上边就是题号那一行：只能往上放，不能往下切进题目开头
                if target is first and new_top > first[1] + 15:
                    manual = True
                    notes.append(f"拒绝把上边下移到“{o['arg']}”（会切掉题目开头），需要手动")
                    continue
                target[1] = new_top
                notes.append(f"上边移到“{o['arg']}”之前")
        elif o["op"] == "续下一页":
            pg = last[0] + 1
            f = _page_img(p_dir, pg)
            if f is None:
                manual = True
                notes.append("下一页不在本书范围内，无法续接")
                continue
            with Image.open(f) as im:
                W, H = im.size
            # 下一页上下一道题开始之前
            idx = order.index(pid) if pid in order else -1
            nxt_top = None
            for q in order[idx + 1:]:
                for b in all_boxes.get(q, []):
                    if b[0] == pg:
                        nxt_top = b[1] if nxt_top is None else min(nxt_top, b[1])
                if nxt_top is not None:
                    break
            y0, y1 = int(H * 0.06), int((nxt_top - 6) if nxt_top else H * 0.94)
            if y1 - y0 > 20:
                boxes.append([pg, y0, y1, last[3], last[4]])
                notes.append(f"续接第 {pg} 页开头")
        elif o["op"] == "去掉续页":
            boxes = [b for b in boxes if b[0] == first[0]] or boxes[:1]
            notes.append("去掉续页部分")
    # 自动修完再贴合墨迹：上下边若切在文字中间，挪到最近的行间空白
    for b in boxes:
        f = _page_img(p_dir, b[0])
        if f is None:
            continue
        import numpy as np
        g = np.asarray(Image.open(f).convert("L"))
        rows = (g[:, b[3]:b[4]] < 160).sum(axis=1) >= 2

        def snap(y, direction):
            y = int(min(max(y, 0), len(rows) - 1))
            for _ in range(60):
                if not rows[y]:
                    return y
                y = min(len(rows) - 1, max(0, y + direction))
            return y
        b[2] = snap(b[2], 1)
        b[1] = snap(b[1], -1)
    return boxes, notes, manual


def run_fix(p_dir: Path, pids: Optional[List[str]] = None, concurrency: int = 3):
    from google.genai import types
    from core import usage
    from core.config import load_book_config
    from core.solver import init_gemini_client, _call, _model_name
    from core.crop_edit import save_boxes
    from core.crop_checker import run_check
    p_dir = Path(p_dir)
    cfg = load_book_config(p_dir)
    model = _model_name(cfg)
    client = init_gemini_client(cfg)
    rep = json.loads((p_dir / "crop_check_report.json").read_text(encoding="utf-8"))
    targets = pids or list(rep.get("flagged", []))
    all_boxes, order, slices = {}, [], {}
    for pj in sorted(p_dir.glob("Chapter_*/problems.json")):
        for prob in json.loads(pj.read_text(encoding="utf-8")):
            pid = str(prob["problem_id"])
            order.append(pid)
            all_boxes[pid] = prob.get("boxes") or []
            slices[pid] = pj.parent / "pages" / f"problem_{pid}_slice.png"
    targets = [t for t in targets if all_boxes.get(t)]
    ov_f = p_dir / "crop_overrides.json"
    prev_override = json.loads(ov_f.read_text(encoding="utf-8")) if ov_f.exists() else {}
    CROPFIX.update(is_running=True, project=p_dir.name, done=0, total=len(targets), message="开始修复…", error=None, results={})
    results: Dict[str, Any] = {}

    def one(pid: str):
        usage.set_context(p_dir, pid)
        issues = "\n".join(f"- 第 {x.get('page')} 页：{x['type']}：{x.get('detail', '')}"
                           for x in rep["results"].get(pid, {}).get("problems", []))
        parts = [PROMPT.format(pid=pid, issues=issues or "（无具体描述）")]
        for pg in sorted({b[0] for b in all_boxes[pid]} | {all_boxes[pid][-1][0] + 1}):
            img = _overlay(p_dir, pg, all_boxes, pid)
            if img:
                parts += [f"【原书第 {pg} 页】", types.Part.from_bytes(data=img, mime_type="image/png")]
        if slices[pid].exists():
            parts += [f"【{pid} 现在的切图】", types.Part.from_bytes(data=slices[pid].read_bytes(), mime_type="image/png")]
        out = _call(client, model, "你是教材排版修正助手，只输出操作行。", parts, 0.0, tries=4, stage="crop_fix")
        ops = _plan_ops(out, pid)
        if not ops:
            return pid, {"status": "manual", "notes": ["模型没有给出可执行的操作"]}
        if all(o["op"] == "没问题" for o in ops):
            return pid, {"status": "unchanged", "notes": ["模型复核后认为没有问题"]}
        boxes, notes, manual = _apply(p_dir, pid, all_boxes[pid], ops, all_boxes, order)
        if manual and not any(o["op"] in ("下边到", "上边到", "续下一页", "去掉续页") for o in ops):
            return pid, {"status": "manual", "notes": notes}
        if boxes != all_boxes[pid]:
            save_boxes(p_dir, pid, boxes, dpi=int(cfg.get("book", {}).get("dpi", 150)))
            return pid, {"status": "fixed" if not manual else "partly", "notes": notes, "boxes": boxes}
        return pid, {"status": "manual" if manual else "unchanged", "notes": notes}

    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            for fut in [ex.submit(one, t) for t in targets]:
                try:
                    pid, r = fut.result()
                except Exception as e:
                    pid, r = "?", {"status": "error", "notes": [str(e)]}
                results[pid] = r
                CROPFIX["done"] += 1
                CROPFIX["message"] = f"已处理 {CROPFIX['done']}/{CROPFIX['total']} 题"
        fixed = [k for k, v in results.items() if v["status"] in ("fixed", "partly")]
        # 修过的题所在页重新做一次 AI 检查，确认修好了
        if fixed:
            CROPFIX["message"] = f"已修改 {len(fixed)} 题，正在复查…"
            pages = sorted({b[0] for k in fixed for b in results[k].get("boxes", [])} |
                           {b[0] for k in fixed for b in all_boxes[k]})
            run_check(p_dir, only_pages=pages)
        rep2 = json.loads((p_dir / "crop_check_report.json").read_text(encoding="utf-8"))
        still = [k for k in fixed if not rep2.get("results", {}).get(k, {}).get("ok", True)]
        # 复查没通过：改回原来的框，留给人工（只保留确认修好的修改）
        for k in still:
            ov_f = p_dir / "crop_overrides.json"
            ov = json.loads(ov_f.read_text(encoding="utf-8")) if ov_f.exists() else {}
            if k in prev_override:
                save_boxes(p_dir, k, prev_override[k], dpi=int(cfg.get("book", {}).get("dpi", 150)))
            else:
                save_boxes(p_dir, k, all_boxes[k], dpi=int(cfg.get("book", {}).get("dpi", 150)))
                ov = json.loads(ov_f.read_text(encoding="utf-8"))
                ov.pop(k, None)                       # 原来就是自动切的：撤掉人工框标记
                ov_f.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")
            results[k]["status"] = "reverted"
            results[k]["notes"].append("复查仍有问题，已改回原来的切法，请手动调整")
        if still:
            rep3 = json.loads((p_dir / "crop_check_report.json").read_text(encoding="utf-8"))
            for k in still:      # 恢复原来的检查结论
                if k in rep.get("results", {}):
                    rep3["results"][k] = rep["results"][k]
            rep3["flagged"] = sorted(pid for pid, v in rep3["results"].items() if not v.get("ok"))
            (p_dir / "crop_check_report.json").write_text(json.dumps(rep3, ensure_ascii=False, indent=2), encoding="utf-8")
        fixed = [k for k in fixed if k not in still]
        manual = [k for k, v in results.items() if v["status"] == "manual"]
        (p_dir / "crop_fix_report.json").write_text(json.dumps(
            {"fixed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results, "still_flagged": still},
            ensure_ascii=False, indent=2), encoding="utf-8")
        CROPFIX["message"] = (f"修复完成：自动修好 {len(fixed)} 题" +
                              (f"，{len(still)} 题复查没通过已改回原样" if still else "") +
                              f"，{len(manual) + len(still)} 题需要在切题检查页手动调整")
        CROPFIX["results"] = results
    except Exception as e:
        CROPFIX.update(error=str(e), message=f"修复失败：{e}")
    finally:
        CROPFIX["is_running"] = False


def start_fix(p_dir: Path, pids: Optional[List[str]] = None) -> bool:
    with _LOCK:
        if CROPFIX["is_running"]:
            return False
        CROPFIX["is_running"] = True
    threading.Thread(target=run_fix, args=(Path(p_dir), pids), daemon=True).start()
    return True
