# -*- coding: utf-8 -*-
"""
阶段 2：题目数字化与原书版面排布对齐模块
1. 多模态提取 LaTeX 题干与公式
2. OCR 感知小问横向/纵向排布，注入 &emsp;&emsp; 还原真实教材呼吸感
3. 生成 timu/timu_X.Y.md 与 timu/Chapter_XX_Timu_All.md
"""

import os
import re
import json
import time
from pathlib import Path
from typing import List, Dict, Any, Tuple
from tqdm import tqdm

from core.config import CHAPTERS_ROOT, load_book_config, get_chapter_config

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:
    RapidOCR = None

from google import genai
from google.genai import types

def init_gemini_client(cfg):
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        return genai.Client(api_key=api_key)
    ai_cfg = cfg.get("ai", {})
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT", ai_cfg.get("project_id", "citric-biplane-358313"))
    loc = os.environ.get("GOOGLE_CLOUD_LOCATION", ai_cfg.get("location", "global"))
    return genai.Client(vertexai=True, project=proj, location=loc)

def detect_horizontal_groups(slice_path: Path, ocr_engine) -> List[List[str]]:
    """检测题目切片中小问的横向分栏排布关系"""
    if not ocr_engine or not slice_path.exists():
        return []
    try:
        res, _ = ocr_engine(str(slice_path))
    except Exception:
        return []
    if not res:
        return []

    sub_items = []
    for bbox, text, score in res:
        matches = list(re.finditer(r'[\(（]([a-lA-L]|\d+)[\)）]', text))
        for m in matches:
            sub = m.group(1).lower()
            ratio = m.start() / max(len(text), 1)
            x_est = bbox[0][0] + ratio * (bbox[1][0] - bbox[0][0])
            y_center = (bbox[0][1] + bbox[2][1]) / 2.0
            sub_items.append({'sub': sub, 'y': y_center, 'x': x_est, 'text': text})

    if len(sub_items) < 2:
        return []

    sub_items.sort(key=lambda it: (it['y'], it['x']))
    groups = []
    for it in sub_items:
        placed = False
        for grp in groups:
            if abs(grp[0]['y'] - it['y']) < 22:
                grp.append(it)
                placed = True
                break
        if not placed:
            groups.append([it])

    horizontal_groups = []
    for grp in groups:
        seen = set()
        uniq = []
        for g in sorted(grp, key=lambda x: x['x']):
            if g['sub'] not in seen:
                seen.add(g['sub'])
                uniq.append(g['sub'])
        if len(uniq) > 1:
            horizontal_groups.append(uniq)

    return horizontal_groups

def parse_problem_text(text: str) -> Tuple[str, List, Dict]:
    norm = re.sub(r'\s*(?:&emsp;+|\\qquad|\\quad)\s*', '\n', text)
    stem_lines = []
    sub_items = []
    current_sub = None

    for line in norm.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        m = re.match(r'^[\(（]([a-lA-L]|\d+)[\)）]\s*(.*)$', line_str)
        if m:
            label = m.group(1).lower()
            content = m.group(2).strip()
            current_sub = (label, [content] if content else [])
            sub_items.append(current_sub)
        else:
            if current_sub is not None:
                current_sub[1].append(line_str)
            else:
                stem_lines.append(line_str)

    sub_map = {item[0]: ' '.join(item[1]).strip() for item in sub_items}
    stem = '\n'.join(stem_lines).strip()
    return stem, sub_items, sub_map

def reformat_problem_text(raw_text: str, horizontal_groups: List[List[str]]) -> str:
    if not horizontal_groups or not raw_text.strip():
        return raw_text

    stem, sub_items, sub_map = parse_problem_text(raw_text)
    if not sub_items:
        return raw_text

    output_lines = []
    if stem:
        output_lines.append(stem)
        output_lines.append("")

    consumed = set()
    for grp in horizontal_groups:
        grp_present = [sub for sub in grp if sub in sub_map and sub not in consumed]
        if len(grp_present) > 1:
            line_parts = []
            for sub in grp_present:
                c = sub_map[sub]
                line_parts.append(f"({sub}) {c}".strip())
                consumed.add(sub)
            output_lines.append(" &emsp;&emsp; ".join(line_parts))
            output_lines.append("")

    for sub, c_list in sub_items:
        if sub not in consumed:
            c = sub_map.get(sub, '')
            output_lines.append(f"({sub}) {c}".strip())
            output_lines.append("")
            consumed.add(sub)

    return "\n".join(output_lines).strip()

def digitize_chapter(chapter_num: int, force: bool = False):
    cfg = load_book_config()
    ch_info = get_chapter_config(chapter_num, cfg)
    ch_dir = CHAPTERS_ROOT / f"Chapter_{chapter_num:02d}"
    pages_dir = ch_dir / "pages"
    timu_dir = ch_dir / "timu"
    timu_dir.mkdir(parents=True, exist_ok=True)
    problems_json = ch_dir / "problems.json"

    ocr_engine = RapidOCR() if RapidOCR else None
    client = init_gemini_client(cfg)

    # 1. 检查或生成 problems.json
    problems = []
    if problems_json.exists() and not force:
        with open(problems_json, "r", encoding="utf-8") as f:
            problems = json.load(f)
    else:
        page_files = sorted(pages_dir.glob("page_*.png"), key=lambda x: int(x.stem.split("_")[1]))
        print(f"[*] 正在多模态扫描第 {chapter_num} 章习题页以提取文本（共 {len(page_files)} 页）...")

        for pf in tqdm(page_files, desc="提取题干文本", unit="页", ascii=True):
            pno = int(pf.stem.split("_")[1])
            with open(pf, "rb") as f:
                img_bytes = f.read()

            prompt = f"""
            你是一名专业的数字化助教。请仔细分析本页《奥本海默·信号与系统》中文版教材习题页面（当前页码：第 {pno} 页，第 {chapter_num} 章）。
            提取本页出现的全部习题（如 {chapter_num}.1, {chapter_num}.2 等）。
            严格输出为纯 JSON 数组格式：
            [
              {{
                "problem_id": "{chapter_num}.1",
                "text": "题目的完整题干与公式（使用规范LaTeX）",
                "page": {pno}
              }}
            ]
            仅输出 JSON，不要任何多余标记。
            """
            for attempt in range(3):
                try:
                    resp = client.models.generate_content(
                        model=cfg.get("ai", {}).get("model", "gemini-3.8-flash"),
                        contents=[types.Part.from_bytes(data=img_bytes, mime_type="image/png"), prompt],
                        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
                    )
                    items = json.loads(resp.text)
                    for it in items:
                        it["page"] = pno
                        problems.append(it)
                    break
                except Exception:
                    time.sleep(1 + attempt)

        # 排序并去重
        seen_pids = set()
        dedup_problems = []
        for p in problems:
            pid = str(p.get("problem_id", "")).strip()
            if pid and pid not in seen_pids:
                seen_pids.add(pid)
                dedup_problems.append(p)

        def sort_key(it):
            m = re.match(rf'{chapter_num}\.(\d+)', str(it["problem_id"]))
            return int(m.group(1)) if m else 999
        dedup_problems.sort(key=sort_key)
        problems = dedup_problems

        with open(problems_json, "w", encoding="utf-8") as f:
            json.dump(problems, f, ensure_ascii=False, indent=2)

    print(f"[*] 第 {chapter_num} 章共有 {len(problems)} 道题，开始生成 timu 细节与版式对齐...")

    # 2. 生成单个 timu_*.md 与 Chapter_XX_Timu_All.md
    all_md_lines = [
        f"# 奥本海默《信号与系统》（第2版）第 {chapter_num} 章 习题全书（含原书高清原图）\n\n",
        f"> 本文档汇集第 {chapter_num} 章全部习题。每道题均完整包含原书高清原图与标准 LaTeX 文本。\n\n"
    ]

    for p in problems:
        pid = p["problem_id"]
        page = p.get("page", 0)
        raw_text = p.get("text", "").strip()

        # 版面感知与排版优化
        slice_path = pages_dir / f"problem_{pid}_slice.png"
        horizontal_groups = detect_horizontal_groups(slice_path, ocr_engine)
        aligned_text = reformat_problem_text(raw_text, horizontal_groups)

        img_ref = f"![](../pages/problem_{pid}_slice.png)" if slice_path.exists() else f"![](../pages/page_{page}.png)"

        single_md = f"""# 习题 {pid}

- **所属章节**：第 {chapter_num} 章
- **原书页码**：第 {page} 页

{img_ref}

## 📝 题目文本与公式

{aligned_text}
"""
        single_path = timu_dir / f"timu_{pid}.md"
        with open(single_path, "w", encoding="utf-8") as f:
            f.write(single_md)

        all_md_lines.append(f"## 习题 {pid} (第 {page} 页)\n\n{img_ref}\n\n### 📝 题目文本\n\n{aligned_text}\n\n---\n\n")

    all_path = timu_dir / f"Chapter_{chapter_num:02d}_Timu_All.md"
    with open(all_path, "w", encoding="utf-8") as f:
        f.write("".join(all_md_lines))

    print(f"[OK] 第 {chapter_num} 章题目数字化与版式对齐完成！已写入 {timu_dir}")
