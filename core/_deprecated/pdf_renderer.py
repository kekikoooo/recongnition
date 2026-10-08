# -*- coding: utf-8 -*-
"""
阶段 1.1：PDF 页面高清栅格化模块
将教材对应章节的习题物理页面渲染为高清 PNG 图像
"""

import sys
from pathlib import Path
from tqdm import tqdm
import pymupdf

from core.config import PROJECT_ROOT, CHAPTERS_ROOT, load_book_config, find_pdf_path, get_chapter_config

def render_chapter_pages(chapter_num: int, dpi: int = None, force: bool = False):
    cfg = load_book_config()
    pdf_path = find_pdf_path(cfg)
    ch_info = get_chapter_config(chapter_num, cfg)
    
    if dpi is None:
        dpi = cfg.get("book", {}).get("dpi", 150)
        
    start_p = ch_info["start_page"]
    end_p = ch_info["end_page"]
    ch_name = ch_info.get("name", f"第{chapter_num}章")
    
    ch_dir = CHAPTERS_ROOT / f"Chapter_{chapter_num:02d}"
    pages_dir = ch_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    
    doc = pymupdf.open(str(pdf_path))
    total_pages = end_p - start_p + 1
    
    print(f"[*] 正在为第 {chapter_num} 章（{ch_name}）渲染页面图片（共 {total_pages} 页，DPI={dpi}）...")
    
    rendered_count = 0
    with tqdm(total=total_pages, desc=f"第 {chapter_num} 章切图", unit="页", ascii=True) as pbar:
        for p in range(start_p, end_p + 1):
            out_img = pages_dir / f"page_{p}.png"
            if force or not out_img.exists():
                # pymupdf load_page 是 0-indexed，传入 p - 1
                page = doc.load_page(p - 1)
                pix = page.get_pixmap(dpi=dpi)
                pix.save(str(out_img))
                rendered_count += 1
            pbar.update(1)
            
    doc.close()
    print(f"[OK] 第 {chapter_num} 章页面图像就绪：新增/更新 {rendered_count} 页，目录：{pages_dir}")
    return pages_dir

def render_all_chapters(dpi: int = None, force: bool = False):
    cfg = load_book_config()
    chapters = cfg.get("chapters", {})
    for ch_key in chapters.keys():
        render_chapter_pages(int(ch_key), dpi=dpi, force=force)
    print(f"[🎉] 全书 {len(chapters)} 个章节页面切图全部就绪！")
