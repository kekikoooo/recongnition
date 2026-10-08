# -*- coding: utf-8 -*-
"""
StudyHelp 通用切题入口 (UniversalCropper)

按 profile.json 的 doc_type 分流：
- book : 教材习题集（扫描版或文字版）-> OcrBookCropper（题号连续性锚定 + 几何切割 + 跨页拼接）
- exam : 试卷 -> LegacyExamCropper（旧版文字层状态机，保持原有试卷行为不变）
旧版完整实现保留在 core/legacy_exam_cropper.py，原文件备份为 cropper.py.bak_before_ocr。
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import find_pdf_path


class UniversalCropper:
    def __init__(self, pdf_path: Optional[str] = None, dpi: int = 150,
                 page_range: Optional[Tuple[int, int]] = None, workers: Optional[int] = None, log=print,
                 only_pages: Optional[set] = None, expected_ids: Optional[set] = None):
        if pdf_path:
            p = Path(pdf_path)
            self.pdf_path = find_pdf_path(p) if p.is_dir() else p
        else:
            self.pdf_path = find_pdf_path()
        if not self.pdf_path.exists():
            raise FileNotFoundError(f"PDF 文档未找到: {self.pdf_path}")
        self.dpi = dpi
        self.page_range = page_range
        self.workers = workers
        self.log = log
        self.only_pages = only_pages
        self.expected_ids = expected_ids

    def _doc_type(self) -> str:
        pf = self.pdf_path.parent / "profile.json"
        if pf.exists():
            try:
                return json.loads(pf.read_text(encoding="utf-8")).get("book", {}).get("doc_type", "exam")
            except Exception:
                pass
        return "exam"

    def _has_text_layer(self) -> bool:
        import pymupdf
        with pymupdf.open(str(self.pdf_path)) as d:
            n = min(len(d), 4)
            return sum(len(d[i].get_text().strip()) for i in range(n)) > 40 * max(1, n)

    def crop_all(self) -> Dict[str, List[Dict[str, Any]]]:
        from core.ocr_book_cropper import OcrBookCropper
        if self._doc_type() == "book":
            return OcrBookCropper(str(self.pdf_path), dpi=self.dpi, workers=self.workers,
                                  page_range=self.page_range, log=self.log, only_pages=self.only_pages,
                                  expected_ids=self.expected_ids).run()
        if not self._has_text_layer():
            # 扫描版试卷：旧版文字层切题器无法处理，走 OCR 切题（按“一、选择题”等大题分组）
            return OcrBookCropper(str(self.pdf_path), dpi=self.dpi, workers=self.workers,
                                  page_range=self.page_range, exam=True, log=self.log, only_pages=self.only_pages).run()
        from core.legacy_exam_cropper import LegacyExamCropper
        return LegacyExamCropper(str(self.pdf_path), dpi=self.dpi).crop_all()
