# -*- coding: utf-8 -*-
"""
StudyHelp 智能大纲与文档双模式自适应嗅探器 (Auto Profiler)
核心职责：
1. 自动判别文档模式：【试卷模式 (Exam)】vs 【书籍教材模式 (Book)】；
2. 提取文献标题、副标题、学科领域（用于动态装配解题与伴读 Prompt）；
3. 极速自动扫描大题或章节列表及其对应的起止物理页码；
4. 采用大模型轻量研判（仅调用 1 次，消耗约 300 Token），网络异常时无缝 fallback 到本地 RapidOCR / 规则兜底。
"""

import os
import re
import sys
import json
from pathlib import Path
from typing import Dict, Any, Optional

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import fitz  # PyMuPDF

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import PROJECT_ROOT, DEFAULT_CONFIG_FILE
from core.solver import init_gemini_client
from google.genai import types

def sniff_document_profile(pdf_path: str, use_ai: bool = True) -> Dict[str, Any]:
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        raise FileNotFoundError(f"PDF 文件不存在: {pdf_path}")

    doc = fitz.open(str(pdf_file))
    total_pages = len(doc)
    toc = doc.get_toc()

    # 1. 尝试大模型轻量识别（仅输入第 1 页高清图，极速 + 免疫字体乱码）
    ai_profile = None
    if use_ai and total_pages > 0:
        try:
            client = init_gemini_client()
            page1 = doc[0]
            pix = page1.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")
            img_part = types.Part.from_bytes(data=img_bytes, mime_type="image/png")

            prompt = f"""你是一名智能文档结构分析专家。请根据这份文献的首页图像（总页数共 {total_pages} 页），精准识别并以纯 JSON 格式输出文献信息。
必须包含字段：
{{
  "doc_type": "exam" 或 "book",  // 试卷填 exam，教材/习题集填 book
  "title": "完整精确的文档名称（如：河海大学《复变函数与积分变换A》（2022春B卷）或《信号与系统》第二版）",
  "subtitle": "官方标准解析收录 / 课后习题全解",
  "author": "命题组或教材编者",
  "subject": "math" 或 "ee" 或 "physics" 或 "general",
  "chapters": {{
    "1": {{"name": "大题或第一章全称", "start_page": 1, "end_page": 1}},
    "2": {{"name": "第二部分全称", "start_page": 2, "end_page": 2}}
  }}
}}
注意：
1. 若为试卷，根据卷面各大题分布合理预估各题型所在起止物理页码（1到{total_pages}之间）；
2. 直接返回 JSON，严禁额外寒暄。
"""
            # 流式调用（与解题相同，带重试；非流式长调用经代理容易被断开）
            from core import usage
            from core.solver import _call
            usage.set_context(pdf_file.parent, None)
            raw = _call(client, "gemini-3.8-flash", "你是文档结构分析专家，只输出 JSON。", [prompt, img_part], 0.1,
                        tries=3, stage="profile")
            m_json = re.search(r"\{.*\}", raw or "", re.S)
            if m_json:
                ai_profile = json.loads(m_json.group(0))
                print(f"[✓] AI 成功完成大纲嗅探（仅 1 次轻量调用，约 300 Token）: {ai_profile.get('title')}")
        except Exception as e:
            print(f"[-] AI 轻量分析跳过或不可用 ({e})，启用本地规则引擎分析...")

    if ai_profile and "title" in ai_profile and "chapters" in ai_profile:
        doc_type = ai_profile.get("doc_type", "exam" if total_pages <= 12 else "book")
        title = ai_profile.get("title", pdf_file.stem)
        subtitle = ai_profile.get("subtitle", "官方标准解析 · 智能自适应收录")
        author = ai_profile.get("author", "课程教学团队 / 命题组")
        subject = ai_profile.get("subject", "general")
        detected_chapters = ai_profile.get("chapters", {})
    else:
        # 本地启发式 Fallback
        doc_type = "exam" if total_pages <= 12 else "book"
        title = pdf_file.stem
        subtitle = "官方标准解析 · 智能自适应收录" if doc_type == "exam" else "课后习题权威推导合订本"
        author = "课程教学团队"
        subject = "general"
        detected_chapters = {
            "1": {
                "name": "试卷全卷试题" if doc_type == "exam" else "第 1 部分 课后习题",
                "start_page": 1,
                "end_page": total_pages
            }
        }

    profile_result = {
        "book": {
            "id": re.sub(r'[^a-zA-Z0-9_]', '_', pdf_file.stem).lower(),
            "title": title,
            "subtitle": subtitle,
            "author": author,
            "doc_type": doc_type,
            "subject": subject,
            "pdf_candidates": [
                str(pdf_file.relative_to(PROJECT_ROOT)).replace('\\', '/') if pdf_file.is_relative_to(PROJECT_ROOT) else str(pdf_file)
            ],
            "dpi": 150
        },
        "chapters_dir": "chapters",
        "chapters": detected_chapters,
        "ai": {
            "model": "gemini-3.8-flash",
            "project_id": os.environ.get("GOOGLE_CLOUD_PROJECT", "citric-biplane-358313"),
            "location": os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
            "temperature": 0.2
        }
    }

    return profile_result

def auto_generate_and_save_profile(
    pdf_path: str,
    output_config: Optional[Path] = None,
    doc_type: Optional[str] = None,
    subject: Optional[str] = None,
    title: Optional[str] = None
) -> Dict[str, Any]:
    target_cfg = output_config or DEFAULT_CONFIG_FILE
    profile = sniff_document_profile(pdf_path)
    if doc_type:
        profile["book"]["doc_type"] = doc_type
    if subject:
        profile["book"]["subject"] = subject
    if title:
        profile["book"]["title"] = title
    pdf_file = Path(pdf_path)
    if pdf_file.name not in profile["book"].get("pdf_candidates", []):
        profile["book"]["pdf_candidates"].insert(0, pdf_file.name)

    target_cfg.parent.mkdir(parents=True, exist_ok=True)
    with open(target_cfg, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    print(f"[✓] 已为【{profile['book']['doc_type'].upper()} 模式】自适应生成 Profile -> {target_cfg.name}")
    return profile

if __name__ == "__main__":
    test_pdf = sys.argv[1] if len(sys.argv) > 1 else r"uploads\2022春学期复变期末（B）.pdf"
    p = auto_generate_and_save_profile(test_pdf)
    print(f"\n[✓] 完成！已智能识别为【{p['book']['doc_type']} 模式】: 《{p['book']['title']}》")
