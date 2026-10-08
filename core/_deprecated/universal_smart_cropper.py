# -*- coding: utf-8 -*-
"""
StudyHelp 通用题目智能识别与自适应矢量裁切引擎 (Universal Smart Problem Cropper)
核心设计目标：
1. 自动识别题目锚点（支持试卷与教材的各种单级/多级题号模式）
2. 自动归并该题全部文本、选项、分式、公式与图形的物理边界
3. 智能识别大题下方的答题空白区并紧凑截断（绝不截出大片纯白纸）
4. 保持自然试卷/教材版面宽度（正常留白，不生硬割裂版芯）
"""

import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
import fitz  # PyMuPDF

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

class UniversalSmartCropper:
    """通用智能切题引擎"""

    def __init__(self, pdf_path: str, dpi: int = 200):
        self.pdf_path = Path(pdf_path)
        if not self.pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        self.doc = fitz.open(str(self.pdf_path))
        self.dpi = dpi
        self.total_pages = len(self.doc)

    def extract_and_crop_all_problems(self, output_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
        """
        全自动流水线：
        1. 遍历每一页，抽取行级文字与其精确坐标
        2. 状态机识别题号锚点与题型大题
        3. 聚合并计算每道题的精确外接矩形 [x0, y0, x1, y1]
        4. 导出高质量自然排版切片
        """
        all_problems = []
        current_section = "未分类部分"
        sec_idx = 1
        global_prob_counter = 1

        # 题号匹配正则（涵盖试卷与教材常用格式）
        # 1. 大题模式：一、选择题 / Part 1 / 一、 / 1、大题
        sec_regex = re.compile(r'^([一二三四五六七八九十]+|[1-9]\d*)\s*[\.、]\s*([^\n\r]+)')
        # 2. 小题模式：1. / 2. / 1.1 / (1) / 习题 1.1
        prob_regex = re.compile(r'^(?:习题\s*|Problem\s*|Ex\s*|【\s*)?(\d+(?:\.\d+)?)\s*[\.、\s\):：](.*)', re.IGNORECASE)

        for pno in range(self.total_pages):
            page = self.doc[pno]
            page_w = page.rect.width
            page_h = page.rect.height

            # 1. 提取结构化行 (Line Items)
            blocks = page.get_text("blocks")
            # 过滤掉极靠近边缘的页眉与页脚
            content_blocks = [
                b for b in blocks 
                if b[1] >= 60 and b[3] <= page_h - 40 and len(b[4].strip()) > 0
            ]

            # 按照从上到下的 y 坐标严格排序
            content_blocks.sort(key=lambda b: (round(b[1] / 5) * 5, b[0]))

            # 2. 识别该页内的题目锚点（严格行首 + 左边距限制，防止选项分母误触）
            page_items = []
            for b in content_blocks:
                text = b[4].strip()
                bx0 = b[0]

                # 检查是否为大题标题行
                if bx0 < 120 and any(kw in text for kw in ["选择题", "填空题", "计算题", "综合题", "解答题", "证明题", "大题", "Part", "Section"]):
                    page_items.append({"type": "section_header", "text": text, "block": b})
                    continue

                # 检查是否为小题题号（支持首行带得分等前缀，如 \x1a©\n1. ）
                m = re.search(r'(?:^|\n)\s*(\d{1,2}(?:\.\d{1,2})?)\s*[\.、\s](.*)', text)
                if m and bx0 < 110 and not any(text.strip().startswith(opt) for opt in ["A.", "B.", "C.", "D."]):
                    raw_num = m.group(1)
                    page_items.append({
                        "type": "problem_start",
                        "raw_num": raw_num,
                        "text": text,
                        "block": b
                    })
                else:
                    page_items.append({
                        "type": "content",
                        "text": text,
                        "block": b
                    })

            # 3. 计算每个题目的精确纵坐标闭包
            prob_indices = [i for i, it in enumerate(page_items) if it["type"] == "problem_start"]

            for idx, p_idx in enumerate(prob_indices):
                item = page_items[p_idx]
                raw_num = item["raw_num"]

                # 确定大题归属
                # 寻找本题之前最近的大题头
                preceding_secs = [it for it in page_items[:p_idx] if it["type"] == "section_header"]
                if preceding_secs:
                    current_section = preceding_secs[-1]["text"].split("\n")[0]
                    # 如果大题发生变化，增加大题序号
                    sec_names = [p.get("section_name") for p in all_problems]
                    if current_section not in sec_names and len(all_problems) > 0:
                        sec_idx += 1

                # 确定该题包含的所有 blocks
                if idx + 1 < len(prob_indices):
                    next_p_idx = prob_indices[idx + 1]
                    # 截止到下一个题目之前
                    this_prob_blocks = [page_items[j]["block"] for j in range(p_idx, next_p_idx) if page_items[j]["type"] != "section_header"]
                else:
                    # 本页最后一题，截止到本页最后一个有效 block
                    this_prob_blocks = [page_items[j]["block"] for j in range(p_idx, len(page_items)) if page_items[j]["type"] != "section_header"]

                if not this_prob_blocks:
                    this_prob_blocks = [item["block"]]

                # 精确计算 Bounding Box
                # y0: 本题起始 block 的顶部 - 8pt 呼吸边距
                y0 = max(0.0, min(b[1] for b in this_prob_blocks) - 8.0)
                # y1: 本题最后一个有效 block 的底部 + 8pt 呼吸边距（自动截断大片作答空白！）
                y1 = min(page_h, max(b[3] for b in this_prob_blocks) + 8.0)

                # 横向 x0, x1: 使用正常的自然试卷版芯宽度（左右保留 40~50pt 自然边距）
                x0 = max(0.0, 45.0)
                x1 = min(page_w, page_w - 45.0)

                clip_rect = fitz.Rect(x0, y0, x1, y1)

                # 生成标准题目 ID (如 1.1, 1.2 或 3.1)
                pid = raw_num if "." in raw_num else f"{sec_idx}.{raw_num}"

                full_text = "\n".join([b[4].strip() for b in this_prob_blocks if not b[4].strip().isdigit()])

                prob_data = {
                    "problem_id": pid,
                    "section_idx": sec_idx,
                    "section_name": current_section,
                    "page": pno + 1,
                    "rect": [round(clip_rect.x0, 1), round(clip_rect.y0, 1), round(clip_rect.x1, 1), round(clip_rect.y1, 1)],
                    "height": round(y1 - y0, 1),
                    "text": full_text
                }
                all_problems.append(prob_data)

                # 如果指定了输出目录，执行高清矢量切片渲染
                if output_dir:
                    out_path = Path(output_dir) / f"Chapter_{sec_idx:02d}" / "pages" / f"problem_{pid}_slice.png"
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    pix = page.get_pixmap(clip=clip_rect, dpi=self.dpi)
                    pix.save(str(out_path))

        return all_problems

if __name__ == "__main__":
    test_pdf = r"C:\Users\a5994\OneDrive - HHU\本科课程\本科其他课程\数学\复变\试卷\21-22答案\2022春学期复变期末（B）.pdf"
    if Path(test_pdf).exists():
        cropper = UniversalSmartCropper(test_pdf, dpi=200)
        out_dir = Path("K:/AI/aoben/test2/chapters_fubian")
        probs = cropper.extract_and_crop_all_problems(output_dir=out_dir)
        print(f"成功自适应识别并紧凑裁切全卷 {len(probs)} 道题目:")
        for p in probs:
            print(f" - [{p['problem_id']}] 页面:{p['page']} 高度:{p['height']}pt | 区域:{p['rect']} | 文本前30字: {p['text'][:30].replace(chr(10), ' ')}")
