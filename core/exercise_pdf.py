# -*- coding: utf-8 -*-
"""
习题页拆分：切题完成后，把“只含习题的那些页”从整本书里单独拆成 PDF（本地处理，不花 token）。

给对话框的大模型转写题目时，整本书（几百页、几百道题）一次输出会被长度上限截断；
拆成“全部习题页”和“每章习题页”后，PDF 小很多，一章一章传，每次输出都短，不会被截断。
用法：切题 -> 下载习题页 PDF -> 上传到对话框 + 粘贴提示词 -> 把输出导入。
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

DIR_NAME = "习题页"


def _source_pdf(p_dir: Path) -> Path:
    for f in sorted(p_dir.glob("*.pdf")):
        if not f.name.startswith(("Book_", "Chapter_")):
            return f
    raise FileNotFoundError("工作项里没有原始 PDF")


def build(project_dir: Path) -> Dict[str, Any]:
    """按各题的切割框用到的页生成 PDF；返回文件清单。没有切题结果时返回 {"ready": False}。"""
    import pymupdf
    p = Path(project_dir)
    per_ch: Dict[int, set] = {}
    n_prob: Dict[int, int] = {}
    for pj in sorted(p.glob("Chapter_*/problems.json")):
        ch = int(pj.parent.name.split("_")[1])
        for prob in json.loads(pj.read_text(encoding="utf-8")):
            pages = {int(b[0]) for b in (prob.get("boxes") or [])} or {int(prob.get("page") or 0)}
            per_ch.setdefault(ch, set()).update(x for x in pages if x > 0)
            n_prob[ch] = n_prob.get(ch, 0) + 1
    if not per_ch:
        return {"ready": False}
    names: Dict[str, str] = {}
    try:
        cfg = json.loads((p / "profile.json").read_text(encoding="utf-8"))
        names = {k: v.get("name", "") for k, v in cfg.get("chapters", {}).items()}
    except Exception:
        pass

    out_dir = p / DIR_NAME
    out_dir.mkdir(exist_ok=True)
    src = pymupdf.open(str(_source_pdf(p)))

    def write(path: Path, pages: List[int]) -> int:
        doc = pymupdf.open()
        for pg in pages:
            if 1 <= pg <= len(src):
                doc.insert_pdf(src, from_page=pg - 1, to_page=pg - 1)
        n = len(doc)
        doc.save(str(path), garbage=3, deflate=True)
        doc.close()
        return n

    all_pages = sorted(set().union(*per_ch.values()))
    files: List[Dict[str, Any]] = []
    f_all = out_dir / "习题页_全部.pdf"
    files.append({"name": f_all.name, "label": "全部习题页", "pages": write(f_all, all_pages),
                  "problems": sum(n_prob.values()), "size_mb": 0})
    for ch in sorted(per_ch):
        f = out_dir / f"习题页_第{ch:02d}章.pdf"
        files.append({"name": f.name, "label": names.get(str(ch)) or f"第{ch}章", "ch": ch,
                      "pages": write(f, sorted(per_ch[ch])), "problems": n_prob[ch], "size_mb": 0})
    src.close()
    for it in files:
        it["size_mb"] = round((out_dir / it["name"]).stat().st_size / 1e6, 1)
    return {"ready": True, "dir": str(out_dir), "files": files, "book_pages_used": len(all_pages)}


def file_path(project_dir: Path, name: str) -> Optional[Path]:
    """只允许取“习题页”目录里的文件（防路径穿越）"""
    base = (Path(project_dir) / DIR_NAME).resolve()
    f = (base / Path(name).name).resolve()
    return f if f.parent == base and f.is_file() and f.suffix.lower() == ".pdf" else None
