# -*- coding: utf-8 -*-
"""
切题后的最终检查（不花 token）：把容易漏掉的问题明确列出来，而不是只写在文件里。

- 切题缺号：每章题号应该连续，缺了哪几号（如 2.19 被当成页眉丢了）；
- 导入对账：文本里有但没切到（切题可能漏了）、切到了但文本里没有（仍由系统看图转写）、题号重复；
- 孤立章号：只有一道题、章号远大于其它章（页眉“§9.1”被读成“89.1”这类）。
结果显示在构建进度条、切题检查页顶部的黄色提示条里。
"""

import json
from pathlib import Path
from typing import List


def compute_notes(project_dir: Path) -> List[str]:
    p = Path(project_dir)
    notes: List[str] = []
    rep_f = p / "crop_report.json"
    chapters = {}
    if rep_f.exists():
        try:
            chapters = json.loads(rep_f.read_text(encoding="utf-8")).get("chapters", {})
        except Exception:
            chapters = {}
    for ch, c in sorted(chapters.items(), key=lambda kv: int(kv[0])):
        miss = c.get("missing") or []
        if miss:
            notes.append("切题缺号：" + "、".join(f"{ch}.{k}" for k in miss) + "（这些题没有切出来，请在切题检查页核对原书页）")
    counts = {ch: c.get("count", 0) for ch, c in chapters.items()}
    solid = [int(k) for k, n in counts.items() if n >= 3]
    if solid:
        top = max(solid)
        lone = [k for k, n in counts.items() if n <= 1 and int(k) > top + 3]
        if lone:
            notes.append("可疑的孤立章号：" + "、".join(f"第{k}章" for k in lone) + "（只有 1 道题，多半是页眉或节标题被误当成题号）")
    imp_f = p / "timu_import_report.json"
    if imp_f.exists():
        try:
            r = json.loads(imp_f.read_text(encoding="utf-8"))
        except Exception:
            r = {}
        if r.get("missing_in_crop"):
            notes.append("导入文本里有、但没切出来：" + "、".join(r["missing_in_crop"]) + "（切题可能漏了这些题）")
        if r.get("missing_in_text"):
            n = r["missing_in_text"]
            notes.append(f"切出来了、但导入文本里没有：{'、'.join(n[:12])}{' 等' if len(n) > 12 else ''}（这些题仍由系统看图转写）")
        if r.get("damaged_in_text"):
            notes.append("导入文本里公式写了一半、已跳过（由系统按原图转写）：" + "、".join(r["damaged_in_text"]))
        if r.get("duplicated_in_text"):
            notes.append("导入文本里题号重复：" + "、".join(r["duplicated_in_text"]))
    return notes
