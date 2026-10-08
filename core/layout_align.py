# -*- coding: utf-8 -*-
"""
题干排版对齐（移植并改进 test1 的 detect_horizontal_groups / reformat_problem_text）

并排关系由代码按原图坐标决定，不让大模型自己判断：
1. 对题目切片做 OCR，找出 (a)(b)(1)(2)… 小问标号的坐标；
2. 纵向坐标相近（同一行）的标号判为“并排”；
3. 按题干原有顺序重排：同一行的小问用 &emsp;&emsp; 连成一行，其余各占一行。
相比 test1：保持小问原始顺序（test1 先输出所有并排组再输出其余，混排时会乱序）；
含独立公式（$$）或多行内容的小问不合并，避免破坏公式渲染。
"""

import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_LABEL_IN_TEXT = re.compile(r"[\(（]\s*([a-zA-Z]|\d{1,2}|[ivx]{1,4})\s*[\)）]")
_LABEL_AT_START = re.compile(r"^\s*[\(（]\s*([a-zA-Z]|\d{1,2}|[ivx]{1,4})\s*[\)）]\s*(.*)$")
_SEP = re.compile(r"\s*(?:&emsp;)+\s*|\s*\\qquad\s*|\s{4,}")

_ocr = None
_ocr_lock = threading.Lock()


def _engine():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
    return _ocr


def detect_label_rows(slice_path: Path, row_tol: Optional[float] = None) -> List[List[str]]:
    """返回按版面从上到下的“行”，每行是该行出现的小问标号（从左到右）。"""
    if not Path(slice_path).exists():
        return []
    with _ocr_lock:
        try:
            res, _ = _engine()(str(slice_path))
        except Exception:
            return []
    if not res:
        return []
    heights = sorted((b[2][1] - b[0][1]) for b, _, _ in res)
    tol = row_tol or max(12.0, 0.6 * heights[len(heights) // 2])
    items = []
    for bbox, text, _ in res:
        for m in _LABEL_IN_TEXT.finditer(text):
            ratio = m.start() / max(len(text), 1)
            x = bbox[0][0] + ratio * (bbox[1][0] - bbox[0][0])
            y = (bbox[0][1] + bbox[2][1]) / 2.0
            items.append((y, x, m.group(1).lower()))
    items.sort()
    rows: List[List[Tuple[float, float, str]]] = []
    for it in items:
        if rows and abs(rows[-1][0][0] - it[0]) < tol:
            rows[-1].append(it)
        else:
            rows.append([it])
    out = []
    for r in rows:
        seen, labels = set(), []
        for _, _, lab in sorted(r, key=lambda t: t[1]):
            if lab not in seen:
                seen.add(lab)
                labels.append(lab)
        out.append(labels)
    return out


def _split_items(text: str):
    """拆成题干行与小问列表 [(标号, [内容行...])]，保持原顺序。"""
    lines: List[str] = []
    for raw in text.splitlines():
        # 先按已有的并排分隔符拆开，再按行首标号归属
        parts = [p for p in _SEP.split(raw) if p.strip()] if _LABEL_IN_TEXT.search(raw) else [raw]
        lines.extend(parts if parts else [raw])
    stem: List[str] = []
    subs: List[Tuple[str, List[str]]] = []
    for ln in lines:
        m = _LABEL_AT_START.match(ln.strip())
        if m:
            subs.append((m.group(1).lower(), [m.group(2).strip()] if m.group(2).strip() else []))
        elif subs:
            subs[-1][1].append(ln.rstrip())
        else:
            stem.append(ln.rstrip())
    return stem, subs


def align_text(text: str, rows: List[List[str]]) -> str:
    if not text or not rows:
        return text
    stem, subs = _split_items(text)
    if len(subs) < 2:
        return text
    row_of: Dict[str, int] = {}
    for i, r in enumerate(rows):
        for lab in r:
            row_of.setdefault(lab, i)
    # 原图里没有任何并排行就不动
    if not any(len(r) > 1 for r in rows):
        return text

    def single_line(content: List[str]) -> bool:
        body = [c for c in content if c.strip()]
        return len(body) <= 1 and not any("$$" in c for c in body)

    out: List[str] = []
    s = "\n".join(stem).strip()
    if s:
        out.extend([s, ""])
    i = 0
    while i < len(subs):
        lab, content = subs[i]
        group = [subs[i]]
        r = row_of.get(lab)
        j = i + 1
        while r is not None and j < len(subs) and row_of.get(subs[j][0]) == r:
            group.append(subs[j])
            j += 1
        if len(group) > 1 and all(single_line(c) for _, c in group):
            out.append(" &emsp;&emsp; ".join(f"({l}) {' '.join(x.strip() for x in c if x.strip())}".strip()
                                            for l, c in group))
            out.append("")
            i = j
            continue
        out.append(f"({lab}) " + (content[0] if content else ""))
        out.extend(content[1:])
        out.append("")
        i += 1
    return "\n".join(out).strip()


def align_problem_text(text: str, slice_path: Path) -> str:
    """只有题干里至少有两个小问时才做 OCR，省时间。"""
    if not text or len(_LABEL_IN_TEXT.findall(text)) < 2:
        return text
    return align_text(text, detect_label_rows(slice_path))
