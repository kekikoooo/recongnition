# -*- coding: utf-8 -*-
"""
StudyHelp 通用文档智能分析与题目矢量级精准切片引擎 (Universal Document Analyzer & Problem Cropper)
支持自动识别试卷（Exam）与章节教材（Book），自适应提取版芯、剔除打分栏/页眉页脚，并导出纯净题目高清切片。
"""

import os
import re
import sys
import json
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
import fitz  # PyMuPDF

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

class UniversalDocAnalyzer:
    """通用文档分析与题目切片引擎"""

    def __init__(self, pdf_path: str, dpi: int = 200):
        self.pdf_path = Path(pdf_path)
        if not self.pdf_path.exists():
            raise FileNotFoundError(f"PDF 文件未找到: {pdf_path}")
        self.doc = fitz.open(str(self.pdf_path))
        self.dpi = dpi
        self.total_pages = len(self.doc)

    def classify_document(self) -> Dict[str, Any]:
        """
        核心算法 1：文档类型智能分类（试卷 vs 章节教材）
        """
        # 1. 提取全文样本与目录
        toc = self.doc.get_toc()
        first_page_text = self.doc[0].get_text() if self.total_pages > 0 else ""
        sample_text = "".join([self.doc[i].get_text() for i in range(min(4, self.total_pages))])

        # 2. 特征模式检测
        exam_keywords = ["试卷", "期末", "期中", "模拟", "真题", "考试", "得分", "阅卷人", "题号", "选择题", "填空题", "计算题", "综合题"]
        exam_score = sum(1 for kw in exam_keywords if kw in sample_text)

        # 匹配大题模式：如 "一、选择题", "一、单选题", "二、填空题", "三、计算题", "四、综合题"
        exam_section_patterns = [
            r'([一二三四五六七八九十]+)\s*[\.、]\s*([^\n\r]+?)(?=\s*[\(（]|\n|\r)',
            r'(Part\s+[I|V|X]+|Section\s+[A-Z])\s*[:\.\s]\s*([^\n\r]+)'
        ]
        detected_sections = []
        for pno in range(self.total_pages):
            ptext = self.doc[pno].get_text()
            for pat in exam_section_patterns:
                for match in re.finditer(pat, ptext):
                    sec_name = match.group(0).strip()
                    if any(kw in sec_name for kw in ["题", "Part", "Section"]):
                        detected_sections.append({
                            "page": pno + 1,
                            "title": sec_name
                        })

        # 判定规则（结合版面密度与页数启发式算法，免疫字体编码乱码）
        # 1. 检查是否存在大题留白作答区（试卷核心特征）
        has_large_gap = False
        for pno in range(min(3, self.total_pages)):
            blocks = self.doc[pno].get_text("blocks")
            if len(blocks) >= 2:
                # 计算相邻 block 之间的垂直间距
                for bi in range(len(blocks) - 1):
                    gap = blocks[bi+1][1] - blocks[bi][3]
                    if gap > 120:  # 超过 120pt 的巨大留白，必为作答区
                        has_large_gap = True
                        break

        is_exam = False
        if self.total_pages <= 12 and (has_large_gap or exam_score >= 2 or len(toc) == 0):
            is_exam = True

        doc_type = "exam" if is_exam else "book"
        
        # 提取标题
        title = self.pdf_path.stem
        # 从首页第一行尝试提取更精准的试卷标题
        first_lines = [line.strip() for line in first_page_text.splitlines() if len(line.strip()) > 4]
        if first_lines and any(kw in first_lines[0] for kw in ["大学", "学年", "学期", "课程", "试卷", "考"]):
            title = first_lines[0]

        return {
            "doc_type": doc_type,
            "title": title,
            "total_pages": self.total_pages,
            "exam_score": exam_score,
            "detected_sections": detected_sections,
            "has_toc": len(toc) > 0
        }

    def detect_page_content_box(self, page: fitz.Page) -> fitz.Rect:
        """
        核心算法 2：自适应版芯检测与噪声剔除（剔除页眉、页脚、侧边打分栏）
        """
        rect = page.rect
        blocks = page.get_text("blocks")
        if not blocks:
            return rect

        # 过滤顶部页眉（y < 80 且包含试卷抬头/页码的区块）
        # 过滤底部页脚（y > page_height - 60 且仅有单个数字页码的区块）
        valid_blocks = []
        for b in blocks:
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()
            # 剔除极靠近底部的单页码
            if y0 > rect.height - 50 and (text.isdigit() or len(text) <= 3):
                continue
            # 剔除极右侧的打分框（通常 x > 465 且包含得分/阅卷人）
            if x0 > rect.width * 0.78 and any(kw in text for kw in ["得分", "阅卷", "评卷", "score"]):
                continue
            valid_blocks.append(b)

        if not valid_blocks:
            return fitz.Rect(50, 80, rect.width - 50, rect.height - 80)

        min_x = max(50.0, min(b[0] for b in valid_blocks) - 4)
        max_x = min(rect.width * 0.82, max(b[2] for b in valid_blocks) + 4) # 严格卡住右边界，防止得分框渗入
        min_y = min(b[1] for b in valid_blocks)
        max_y = max(b[3] for b in valid_blocks)

        return fitz.Rect(min_x, min_y, max_x, max_y)

    def extract_problems_from_exam(self) -> List[Dict[str, Any]]:
        """
        核心算法 3：试卷全自动题目切片与提取（无盲猜 offset，紧凑剔除答题留白）
        """
        problems = []
        current_section = "未分类大题"
        sec_idx = 1
        prob_idx_in_sec = 1

        for pno in range(self.total_pages):
            page = self.doc[pno]
            page_rect = page.rect
            blocks = page.get_text("blocks")
            words = page.get_text("words")

            # 遍历行，寻找题号锚点
            lines = []
            for b in blocks:
                text = b[4].strip()
                if not text:
                    continue
                # 检测大题头（如 一、选择题）
                sec_match = re.match(r'^[一二三四五六七八九十]+[\.、]\s*([^\n\r]+)', text)
                if sec_match:
                    current_section = text.split("\n")[0]
                    sec_idx = len(set(p['section_idx'] for p in problems)) + 1
                    prob_idx_in_sec = 1
                    continue

                # 检测小题号（如 1. 2. 3. 4. 或 1.1 1.2）
                prob_match = re.match(r'^(\d+)[\.、\s]\s*(.*)', text, re.DOTALL)
                if prob_match:
                    p_num = prob_match.group(1)
                    lines.append({
                        "type": "prob_start",
                        "num": p_num,
                        "block": b,
                        "text": text
                    })
                else:
                    lines.append({
                        "type": "content",
                        "block": b,
                        "text": text
                    })

            # 计算各小题的精准 bounding box
            prob_starts = [i for i, l in enumerate(lines) if l["type"] == "prob_start"]
            for idx, p_start_idx in enumerate(prob_starts):
                start_line = lines[p_start_idx]
                pid = f"{sec_idx}.{prob_idx_in_sec}"
                prob_idx_in_sec += 1

                # 本题涉及的所有 blocks
                if idx + 1 < len(prob_starts):
                    end_idx = prob_starts[idx + 1]
                    item_blocks = [lines[j]["block"] for j in range(p_start_idx, end_idx)]
                else:
                    item_blocks = [lines[j]["block"] for j in range(p_start_idx, len(lines))]

                # 计算最小外接矩形
                x0 = 65.0
                x1 = min(460.0, page_rect.width - 80.0) # 剔除右侧打分框
                y0 = max(0.0, min(b[1] for b in item_blocks) - 6.0)
                y1 = min(page_rect.height, max(b[3] for b in item_blocks) + 6.0)

                clip_rect = fitz.Rect(x0, y0, x1, y1)
                full_text = "\n".join([b[4].strip() for b in item_blocks if not b[4].strip().isdigit()])

                problems.append({
                    "problem_id": pid,
                    "section_idx": sec_idx,
                    "section_name": current_section,
                    "page": pno + 1,
                    "rect": [clip_rect.x0, clip_rect.y0, clip_rect.x1, clip_rect.y1],
                    "text": full_text
                })

        return problems

    def export_slices(self, problems: List[Dict[str, Any]], output_root: Path):
        """
        核心算法 4：无损矢量级超高清（200 DPI）切片输出
        """
        output_root = Path(output_root)
        output_root.mkdir(parents=True, exist_ok=True)

        for p in problems:
            pid = p["problem_id"]
            sec_idx = p["section_idx"]
            page_no = p["page"]
            rect = fitz.Rect(*p["rect"])

            ch_dir = output_root / f"Chapter_{sec_idx:02d}" / "pages"
            ch_dir.mkdir(parents=True, exist_ok=True)
            target_slice = ch_dir / f"problem_{pid}_slice.png"

            page = self.doc[page_no - 1]
            pix = page.get_pixmap(clip=rect, dpi=self.dpi)
            pix.save(str(target_slice))

        print(f"[✓] 成功为 {len(problems)} 道题目导出高清纯净切片至 {output_root}！")

if __name__ == "__main__":
    test_pdf = r"C:\Users\a5994\OneDrive - HHU\本科课程\本科其他课程\数学\复变\试卷\21-22答案\2022春学期复变期末（B）.pdf"
    if Path(test_pdf).exists():
        analyzer = UniversalDocAnalyzer(test_pdf)
        info = analyzer.classify_document()
        print("文档智能分类结果:", json.dumps(info, ensure_ascii=False, indent=2))
        probs = analyzer.extract_problems_from_exam()
        print(f"提取出 {len(probs)} 道真题:")
        for p in probs:
            print(f" - [{p['problem_id']}] ({p['section_name']}) 页码:{p['page']} 矩形:{[round(x,1) for x in p['rect']]}")
