# -*- coding: utf-8 -*-
"""
StudyHelp 动态聚合主文件与全书合订本生成器 (Dynamic Aggregator - 纯净典藏版)
特性：
1. 极简纯净标题：卷名/书名不带任何多余修饰词（彻底去除“便携速查”、“省纸版”等杂质）；
2. 彻底剔除命题元数据：不添加“命题团队/解析”、“定位说明”等冗余段落，开门见山直接进入大题；
3. 规范层级架构：
   - H1: 书名 / 试卷原名
   - H2: 大题名称（如 一、选择题）
   - H3: 小题题号（如 第 1.1 题）
   - H4: 分步推导段落（题目重述、核心定理、演算推导、最终结论）
"""

import os
import re
import json
from pathlib import Path
from typing import Optional, Dict, Any

from core.config import CHAPTERS_ROOT, load_book_config, get_chapter_config, get_book_metadata, get_all_chapters, get_active_project_dir

def clean_for_latex(text: str) -> str:
    """清理容易导致 XeLaTeX 报错的字符，分离公式与贴近的标点"""
    if not text:
        return ""
    # 分离 $$ 后紧贴的标点
    text = re.sub(r'\$\$([\s\S]+?)\$\$([，。、；,.;!?])', r'$$\n\1\n$$\n\2\n', text)
    # 替换不规范的 tag
    text = re.sub(r'\\tag\{([^}]+)\}', r'\\quad \\text{(\1)}', text)
    return text

def normalize_solution_headings(content: str, is_compact: bool = False) -> str:
    """
    清洗题目解析中的冗余标题，保证 H3/H4 层级严格一致
    """
    if not content:
        return ""
    text = content.strip()

    # 1. 剔除开头的重复题号大标题（例如 "## 习题 1.1 详尽答题规范全解" 或 "## 习题 1.1 核心结论与速查"）
    text = re.sub(r'^##\s+(?:习题|题目)\s*[\d\.]+\s*.*?\n+', '', text)

    # 2. 将正文中的三级小节标题（### 📝 题目文本 等）降级为四级（#### 📝 题目文本），保持清晰从属关系
    text = re.sub(r'^###\s+', '#### ', text, flags=re.MULTILINE)

    return clean_for_latex(text)

def aggregate_chapter(ch_key: Any, cfg: Optional[Any] = None, project_dir: Optional[Path] = None):
    if isinstance(cfg, (str, Path)):
        project_dir = Path(cfg)
        cfg = load_book_config(project_dir)
    elif cfg is None:
        cfg = load_book_config(project_dir)
    
    p_dir = Path(project_dir) if project_dir else get_active_project_dir()
    book_meta = get_book_metadata(cfg)
    ch_info = get_chapter_config(ch_key, cfg)
    ch_idx = int(ch_key) if str(ch_key).isdigit() else 1
    ch_dir = p_dir / f"Chapter_{ch_idx:02d}"
    slots_dir = ch_dir / "slots"
    problems_json = ch_dir / "problems.json"

    if not slots_dir.exists():
        return

    problems = []
    if problems_json.exists():
        with open(problems_json, "r", encoding="utf-8") as f:
            problems = json.load(f)

    # 收集已完成的 slots
    slots_map = {}
    for sf in slots_dir.glob("slot_*.json"):
        try:
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
                slots_map[str(data.get("problem_id"))] = data
        except Exception:
            pass

    ch_name = ch_info.get("name", f"第 {ch_key} 部分")
    book_title = book_meta.get("title", "题库合订本")

    all_md_path = ch_dir / f"Chapter_{ch_idx:02d}_All.md"
    compact_md_path = ch_dir / f"Chapter_{ch_idx:02d}_Compact_Solutions.md"
    print_md_path = ch_dir / f"Chapter_{ch_idx:02d}_Print.md"

    all_lines = [
        f"# {book_title}\n\n",
        f"## {ch_name}\n\n",
    ]

    compact_lines = [
        f"# {book_title}\n\n",
        f"## {ch_name}\n\n",
    ]

    # 按题目清单顺序追加
    for p in problems:
        pid = str(p["problem_id"])
        slot = slots_map.get(pid, {})
        final_content = slot.get("final") or slot.get("stage3") or slot.get("stage3_deduction") or ""
        compact_content = slot.get("compact") or slot.get("compact_breakdown") or slot.get("stage2") or ""

        if final_content:
            all_lines.append(f"### 第 {pid} 题\n\n")
            if slot.get("status") in ("needs_review", "unreviewed"):
                sc = slot.get("score")
                note = f"评审 {sc} 分，未达 95 分" if sc not in ("", None) else "评审未给出分数"
                all_lines.append(f"> **【待人工复核】** {note}，以下解答仅供参考。\n\n")
            all_lines.append(normalize_solution_headings(final_content, is_compact=False))
            all_lines.append("\n\n---\n\n")

        if compact_content:
            compact_lines.append(f"### 第 {pid} 题\n\n")
            compact_lines.append(normalize_solution_headings(compact_content, is_compact=True))
            compact_lines.append("\n\n---\n\n")

    with open(all_md_path, "w", encoding="utf-8") as f:
        f.write("".join(all_lines))

    with open(compact_md_path, "w", encoding="utf-8") as f:
        f.write("".join(compact_lines))

    # 打印版：沿用 test1 调教过的排版规则（去 Emoji、规范步骤换行、合并零碎短行），不是速查的原样拷贝
    from core.print_md import build_print_md
    with open(print_md_path, "w", encoding="utf-8") as f:
        f.write(build_print_md("".join(compact_lines), ch_idx, book_title))

    print(f"[OK] 第 {ch_key} 大题 ({ch_name}) 聚合完成 -> {all_md_path.name}")

def aggregate_all_book(cfg: Optional[Any] = None, project_dir: Optional[Path] = None):
    """
    聚合全书 3 大核心出版级 Markdown 合订本 (纯净无赘言版)
    1. Book_All.md: 满分全解合订本
    2. Book_Print.md: 速查打印合订本
    3. Book_Timu_All.md: 原卷高清书影合订本
    """
    if isinstance(cfg, (str, Path)):
        p_dir = Path(cfg)
        cfg = load_book_config(p_dir)
    elif cfg is None:
        p_dir = Path(project_dir) if project_dir else get_active_project_dir()
        cfg = load_book_config(p_dir)
    else:
        p_dir = Path(project_dir) if project_dir else get_active_project_dir()

    book_meta = get_book_metadata(cfg)
    chapters = get_all_chapters(cfg)

    book_title = book_meta.get("title", "StudyHelp 交互题库")

    book_all_path = p_dir / "Book_All.md"
    book_print_path = p_dir / "Book_Print.md"
    book_timu_path = p_dir / "Book_Timu_All.md"

    # 顶层仅保留纯净唯一的书名/试卷原名，彻底移除修饰词与冗余说明
    book_all_lines = [
        f"# {book_title}\n\n",
    ]

    book_print_lines = [
        f"# {book_title}\n\n",
    ]

    book_timu_lines = [
        f"# {book_title}\n\n",
    ]

    sorted_ch_keys = sorted(chapters.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x))

    for idx, ch_key in enumerate(sorted_ch_keys):
        aggregate_chapter(ch_key, cfg, project_dir=p_dir)

        ch_idx = int(ch_key) if str(ch_key).isdigit() else 1
        ch_dir = p_dir / f"Chapter_{ch_idx:02d}"
        slots_dir = ch_dir / "slots"
        problems_json = ch_dir / "problems.json"
        ch_name = get_chapter_config(ch_key, cfg).get("name", f"第 {ch_key} 大题")

        # 加载 slots
        slots_map = {}
        if slots_dir.exists():
            for sf in slots_dir.glob("slot_*.json"):
                try:
                    with open(sf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        slots_map[str(data.get("problem_id"))] = data
                except Exception:
                    pass

        # 加载 problems
        problems = []
        if problems_json.exists():
            with open(problems_json, "r", encoding="utf-8") as f:
                problems = json.load(f)

        # 1. 汇编 Book_All.md
        if idx > 0:
            book_all_lines.append("\n\n<div style=\"page-break-after: always;\"></div>\n\n")
        book_all_lines.append(f"## {ch_name}\n\n")

        for p in problems:
            pid = str(p["problem_id"])
            slot = slots_map.get(pid, {})
            final_content = slot.get("final") or slot.get("stage3") or slot.get("stage3_deduction") or ""
            if final_content:
                book_all_lines.append(f"### 第 {pid} 题\n\n")
                book_all_lines.append(normalize_solution_headings(final_content, is_compact=False))
                book_all_lines.append("\n\n---\n\n")

        # 2. 汇编 Book_Print.md：把各章打印版依次合并（和 test1 一致：每章一个一级标题，去掉章内原标题）
        pm = ch_dir / f"Chapter_{ch_idx:02d}_Print.md"
        if pm.exists():
            lines = pm.read_text(encoding="utf-8").splitlines()
            st = 0
            if lines and lines[0].startswith("#"):
                st = 1
                while st < len(lines) and not lines[st].strip():
                    st += 1
            book_print_lines.append(f"\n\n# {ch_name}\n\n")
            book_print_lines.append("\n".join(lines[st:]))
            book_print_lines.append("\n\n")


        # 3. 汇编 Book_Timu_All.md
        if idx > 0:
            book_timu_lines.append("\n\n<div style=\"page-break-after: always;\"></div>\n\n")
        book_timu_lines.append(f"## {ch_name}\n\n")

        for p in problems:
            pid = str(p["problem_id"])
            slice_rel = f"Chapter_{ch_idx:02d}/pages/problem_{pid}_slice.png"
            book_timu_lines.append(f"### 第 {pid} 题\n\n")
            if (p_dir / slice_rel).exists():
                book_timu_lines.append(f"![]({slice_rel})\n\n")
            else:
                book_timu_lines.append(f"{p.get('text', '').strip()}\n\n")
            book_timu_lines.append("---\n\n")

    with open(book_all_path, "w", encoding="utf-8") as f:
        f.write("".join(book_all_lines))

    with open(book_print_path, "w", encoding="utf-8") as f:
        f.write("".join(book_print_lines))

    with open(book_timu_path, "w", encoding="utf-8") as f:
        f.write("".join(book_timu_lines))

    print(f"[OK] 全书 3 大核心合订本汇编完成: {book_all_path.name} / {book_print_path.name} / {book_timu_path.name}")

if __name__ == "__main__":
    aggregate_all_book()
