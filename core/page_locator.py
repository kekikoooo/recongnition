# -*- coding: utf-8 -*-
"""
习题页定位：扫描版整本书 OCR 很慢，没有导入文件给出“习题页”范围时，先让大模型看书的目录，
找出各章“习题”所在页，只 OCR 这些页（几十页而不是几百页）。

做法：
1. PDF 自带书签里有“习题”条目 -> 直接用（书签页码就是 PDF 页码，不花 token）；
2. 否则一次模型调用：前若干页（含目录）+ 几张正文页，每张图前标“PDF 第 k 页”。
   模型读出目录里各“习题”的印刷页码范围，以及正文样张上印的页码；
   印刷页码 -> PDF 页码的偏移取最近样张的差值（扫描版中间缺页/插页时偏移会变）。
定位失败或结果不可信 -> 返回 None，调用方照常全书扫描；切完一道题都没有时也会退回全书扫描。
结果存到 exercise_pages.json，重新切题时直接复用。
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pymupdf

RESULT_FILE = "exercise_pages.json"
HEAD_PAGES = 30          # 目录一般在前 30 页内
SAMPLE_FRACS = (0.2, 0.35, 0.5, 0.65, 0.8)
_EX_RE = re.compile(r"习\s*题|练\s*习|思考题|作\s*业\s*题|problems|exercises", re.I)
_ANS_RE = re.compile(r"答\s*案|解\s*答|提\s*示|answers?", re.I)

PROMPT = """下面是一本扫描版教材的部分页面，每张图前标了它在 PDF 文件里的页序号（“PDF 第 k 页”，从 1 起）。
前面若干张是书的开头（封面/前言/目录），后面几张是从正文中间抽的样张。

请完成两件事：
1. 找到目录，列出每一处“习题”小节（标题含 习题 / 练习 / 思考题与习题 / 习题与思考题 等，只要课后习题，
   不要“习题答案”“部分习题参考答案”“例题”）在目录里写的起始印刷页码，以及目录里紧接着它的下一个条目的印刷页码
   （习题到那一页为止）。同时给出所属章号和章名。章末习题、节末习题都要列出。
2. 对每一张标了 PDF 页序号的图，读出页眉或页脚上印的阿拉伯数字页码（读不出或是罗马数字就填 null）。

只输出 JSON，不要其他文字：
{
  "toc_found": true,
  "exercises": [{"chapter": 1, "chapter_name": "半导体器件基础", "printed_start": 45, "printed_next": 49}],
  "page_numbers": [{"pdf": 12, "printed": null}, {"pdf": 150, "printed": 138}]
}
目录里根本没有列出习题小节时，exercises 给空数组；没找到目录时 toc_found 填 false。"""


def _from_bookmarks(doc) -> Optional[List[Dict[str, Any]]]:
    toc = doc.get_toc() or []
    flat = [(t[1].strip(), int(t[2])) for t in toc if len(t) >= 3 and t[2] and t[2] > 0]
    out = []
    for i, (title, pg) in enumerate(flat):
        if not _EX_RE.search(title) or _ANS_RE.search(title):
            continue
        nxt = next((p for _, p in flat[i + 1:] if p > pg), pg + 6)
        m = re.search(r"(\d{1,2})", title)
        out.append({"chapter": int(m.group(1)) if m else None, "pdf_start": pg, "pdf_end": max(pg, nxt)})
    return out if len(out) >= 2 else None


def _jpeg(doc, pno: int, dpi: int = 100) -> bytes:
    pix = doc[pno].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return pix.tobytes("jpeg", jpg_quality=70)


def _ask_model(pdf_path: Path, doc, proj_dir: Path, log) -> Optional[Dict[str, Any]]:
    from google.genai import types
    from core.config import load_book_config
    from core.solver import init_gemini_client, _call, _model_name
    from core import usage

    n = len(doc)
    head = list(range(min(n, HEAD_PAGES)))
    samples = sorted({int(n * f) for f in SAMPLE_FRACS} - set(head))
    contents: List[Any] = [PROMPT]
    for p in head + samples:
        contents.append(f"PDF 第 {p + 1} 页：")
        contents.append(types.Part.from_bytes(data=_jpeg(doc, p), mime_type="image/jpeg"))
    cfg = load_book_config(proj_dir)
    usage.set_context(proj_dir, None)
    raw = _call(init_gemini_client(cfg), _model_name(cfg), "你是文档结构分析专家，只输出 JSON。", contents, 0.1,
                tries=3, stage="locate")
    m = re.search(r"\{.*\}", raw or "", re.S)
    return json.loads(m.group(0)) if m else None


def _to_pdf_ranges(ans: Dict[str, Any], n: int, log) -> Optional[List[Dict[str, Any]]]:
    """印刷页码 -> PDF 页码。偏移 = PDF 页序号 - 印刷页码，取印刷页码最近的样张的偏移。"""
    pts = []
    for it in ans.get("page_numbers") or []:
        try:
            pdf, pr = int(it.get("pdf")), it.get("printed")
            if pr is not None and 1 <= pdf <= n:
                pts.append((int(pr), pdf - int(pr)))
        except (TypeError, ValueError):
            continue
    body = [x for x in pts if x[0] > 5]
    if len(body) < 2:
        log(f"    [-] 习题页定位：正文样张上读出的页码太少（{len(body)} 张），放弃")
        return None
    offs = sorted(o for _, o in body)
    spread = offs[-1] - offs[0]
    if spread > 12:
        log(f"    [-] 习题页定位：各页偏移相差 {spread} 页，不可信，放弃")
        return None
    out = []
    for ex in ans.get("exercises") or []:
        try:
            a = int(ex["printed_start"])
            b = int(ex.get("printed_next") or a + 4)
        except (KeyError, TypeError, ValueError):
            continue
        if b < a or b - a > 40:
            b = a + 4
        off = min(body, key=lambda x: abs(x[0] - a))[1]
        out.append({"chapter": ex.get("chapter"), "chapter_name": ex.get("chapter_name") or "",
                    "printed_start": a, "printed_end": b, "pdf_start": a + off, "pdf_end": b + off,
                    "pad": 1 if spread == 0 else min(4, spread + 1)})
    out = [r for r in out if 1 <= r["pdf_start"] <= n]
    return out or None


def locate_exercise_pages(pdf_path, proj_dir, log=print) -> Optional[Dict[str, Any]]:
    """返回 {"pages": [1 基 PDF 页码...], "ranges": [...], "source": "bookmarks"|"model"}；定位不了返回 None。"""
    if os.environ.get("STUDYHELP_LOCATE", "1") == "0":
        return None
    pdf_path, proj_dir = Path(pdf_path), Path(proj_dir)
    cache = proj_dir / RESULT_FILE
    if cache.exists():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("pages"):
                return d
        except Exception:
            pass
    with pymupdf.open(str(pdf_path)) as doc:
        n = len(doc)
        ranges = _from_bookmarks(doc)
        source = "bookmarks"
        if ranges:
            for r in ranges:
                r["pad"] = 1
        else:
            source = "model"
            try:
                ans = _ask_model(pdf_path, doc, proj_dir, log)
            except Exception as e:
                log(f"    [-] 习题页定位调用失败：{e}")
                return None
            if not ans or not ans.get("exercises"):
                log("    [-] 习题页定位：目录里没找到习题小节，全书扫描")
                return None
            ranges = _to_pdf_ranges(ans, n, log)
            if not ranges:
                return None
    pages = set()
    for r in ranges:
        for q in range(r["pdf_start"] - r["pad"], r["pdf_end"] + r["pad"] + 1):
            if 1 <= q <= n:
                pages.add(q)
    if len(pages) > n * 0.6:
        log(f"    [-] 习题页定位：定位到 {len(pages)}/{n} 页，省不了多少，全书扫描")
        return None
    res = {"source": source, "total_pages": n, "pages": sorted(pages), "ranges": ranges}
    cache.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def apply_chapter_names(proj_dir, res: Dict[str, Any]):
    """目录里读到的章名写进 profile（只扫习题页时看不到章首页，切题器拿不到章名）"""
    pf = Path(proj_dir) / "profile.json"
    try:
        cfg = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {"book": {}, "chapters": {}}
    except Exception:
        return
    chs = cfg.setdefault("chapters", {})
    changed = False
    for r in res.get("ranges") or []:
        ch, nm = r.get("chapter"), (r.get("chapter_name") or "").strip()
        if ch and nm and not (chs.get(str(ch)) or {}).get("name"):
            chs.setdefault(str(ch), {})["name"] = f"第{ch}章 {nm}"
            changed = True
    if changed:
        pf.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
