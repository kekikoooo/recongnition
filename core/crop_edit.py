# -*- coding: utf-8 -*-
"""
人工调整切片：在“切题检查”页拖动原页上的框（或增删区域），按新框从原页图重新切出这道题的切片。

- 新框存进 output/<工作项>/crop_overrides.json，之后再“重新切题”也按人工框切，不会被自动规则覆盖；
- 切片变了，题干需要重新对照原图核对：题干来源改为待核对（导入的题干保持不变），
  已有解答标记“题干/切片已改”，网页提示重解；
- AI 切题检查里这道题的结论清掉（已人工确认）。
"""

import json
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image

OVERRIDES = "crop_overrides.json"


def _page_png(p_dir: Path, ch_dir: Path, page: int, dpi: int) -> Path:
    """原页图：章节目录里有就用；没有（新加了别的页）就从 PDF 渲染一张"""
    f = ch_dir / "pages" / f"page_{page}.png"
    if f.exists():
        return f
    import pymupdf
    pdf = next(x for x in sorted(p_dir.glob("*.pdf")) if not x.name.startswith(("Book_", "Chapter_")))
    with pymupdf.open(str(pdf)) as doc:
        doc[page - 1].get_pixmap(dpi=dpi).save(str(f))
    return f


def find_problem(p_dir: Path, pid: str):
    for pj in sorted(Path(p_dir).glob("Chapter_*/problems.json")):
        probs = json.loads(pj.read_text(encoding="utf-8"))
        for i, p in enumerate(probs):
            if str(p.get("problem_id")) == str(pid):
                return pj, probs, i
    return None, None, None


def mask_owner(m, boxes) -> int:
    """一块“扣除”只属于一个框：同一页上和它重叠面积最大的那个框（落在两个框里也只扣其中一个）；都不重叠返回 -1"""
    best, best_a = -1, 0
    for k, b in enumerate(boxes):
        if int(b[0]) != int(m[0]):
            continue
        h = min(m[2], b[2]) - max(m[1], b[1])
        w = min(m[4], b[4]) - max(m[3], b[3])
        if h > 0 and w > 0 and h * w > best_a:
            best, best_a = k, h * w
    return best


def apply_masks(im: Image.Image, page: int, box, masks: List[List[int]], idx: int = 0, boxes=None) -> Image.Image:
    """把“扣除”的矩形在切片里涂白（只处理属于本框的部分）"""
    from PIL import ImageDraw
    d = ImageDraw.Draw(im)
    bx0, by0 = box[3], box[1]
    for m in masks or []:
        if int(m[0]) != int(page):
            continue
        if boxes is not None and mask_owner(m, boxes) != idx:
            continue
        d.rectangle([m[3] - bx0, m[1] - by0, m[4] - bx0 - 1, m[2] - by0 - 1], fill=255)
    return im


def render_boxes(p_dir: Path, ch_dir: Path, boxes: List[List[int]], dpi: int = 150, masks=None) -> Image.Image:
    parts = []
    for idx, (page, y0, y1, x0, x1) in enumerate(boxes):
        with Image.open(_page_png(p_dir, ch_dir, int(page), dpi)) as im:
            W, H = im.size
            y0, y1 = max(0, int(y0)), min(H, int(y1))
            x0, x1 = max(0, int(x0)), min(W, int(x1))
            if y1 - y0 < 4 or x1 - x0 < 4:
                continue
            parts.append(apply_masks(im.convert("L").crop((x0, y0, x1, y1)), page, (page, y0, y1, x0, x1), masks, idx, boxes))
    if not parts:
        raise ValueError("框太小或为空")
    out = Image.new("L", (max(p.width for p in parts), sum(p.height for p in parts)), 255)
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height
    return out


def save_boxes(p_dir: Path, pid: str, boxes: List[List[int]], dpi: int = 150, masks=None) -> Dict[str, Any]:
    p_dir = Path(p_dir)
    pj, probs, i = find_problem(p_dir, pid)
    if pj is None:
        raise KeyError(f"题目 {pid} 不存在")
    boxes = [[int(v) for v in b[:5]] for b in boxes if len(b) >= 5]
    if not boxes:
        raise ValueError("至少要保留一个区域")
    boxes.sort(key=lambda b: (b[0], b[1]))
    ch_dir = pj.parent
    masks = [[int(v) for v in m[:5]] for m in (masks or []) if len(m) >= 5]
    img = render_boxes(p_dir, ch_dir, boxes, dpi, masks)
    img.save(str(ch_dir / "pages" / f"problem_{pid}_slice.png"))

    ov_f = p_dir / OVERRIDES
    ov = json.loads(ov_f.read_text(encoding="utf-8")) if ov_f.exists() else {}
    ov[str(pid)] = {"boxes": boxes, "masks": masks} if masks else boxes
    ov_f.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")

    prob = probs[i]
    prob["boxes"] = boxes
    prob["masks"] = masks
    if prob.get("text_source") not in ("imported",):
        prob["text_source"] = "none"          # 切片变了：题干下次核对时重新对照原图
    pj.write_text(json.dumps(probs, ensure_ascii=False, indent=2), encoding="utf-8")

    sf = ch_dir / "slots" / f"slot_{pid}.json"
    if sf.exists():
        s = json.loads(sf.read_text(encoding="utf-8"))
        s["stale_text"] = True                 # 解答基于旧切片，网页提示重解
        sf.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

    # 切题报告与 AI 检查结论：标记为人工确认
    rep_f = p_dir / "crop_report.json"
    if rep_f.exists():
        rep = json.loads(rep_f.read_text(encoding="utf-8"))
        ch = str(int(ch_dir.name.split("_")[1]))
        rep.setdefault("chapters", {}).setdefault(ch, {}).setdefault("flagged", {})[str(pid)] = ["manual"]
        rep_f.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    cc_f = p_dir / "crop_check_report.json"
    if cc_f.exists():
        cc = json.loads(cc_f.read_text(encoding="utf-8"))
        if str(pid) in cc.get("results", {}):
            cc["results"][str(pid)] = {"ok": True, "problems": [], "manual": True, "pages": cc["results"][str(pid)].get("pages", [])}
            cc["flagged"] = [x for x in cc.get("flagged", []) if x != str(pid)]
            cc_f.write_text(json.dumps(cc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"pid": str(pid), "boxes": boxes, "size": list(img.size), "solved": sf.exists()}


def page_size(p_dir: Path, page: int, dpi: int = 150):
    """新加区域时前端需要页面尺寸：取任一章节目录里的页图，没有就渲染"""
    for ch_dir in sorted(Path(p_dir).glob("Chapter_*")):
        f = ch_dir / "pages" / f"page_{page}.png"
        if f.exists():
            with Image.open(f) as im:
                return f, im.size
    return None, None
