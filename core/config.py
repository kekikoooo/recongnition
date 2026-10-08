# -*- coding: utf-8 -*-
"""
全局配置与用户工程数据管理模块 (Core Configuration & Project Manager)
架构特性：
1. 统一用户数据目录：所有输入 PDF、版面 Profile、切片、题解与最终出版级合订本，统一归集于 output/[工作项名称]/ 独立文件夹；
2. 消除根目录杂质：彻底替代旧版 uploads/ 与 chapters/ 分散目录；
3. 多项目自适应检索：支持根据 --project 参数、环境变量或按修改时间自动嗅探当前活跃项目。
"""

import os
import sys
import glob
import json
from pathlib import Path
from typing import Dict, Any, Optional, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 所有上传的书/试卷及其全部产物：output/<工作项名>/（每个子文件夹是一个独立工作项）
DATA_ROOT = Path(os.environ.get("STUDYHELP_OUTPUT_ROOT", str(PROJECT_ROOT / "output")))

# 确保用户数据根目录存在
DATA_ROOT.mkdir(parents=True, exist_ok=True)

def _usage_total(d: Path) -> Optional[int]:
    """该工作项累计 token（没有记账记录的旧项目返回 None）"""
    try:
        from core.usage import summarize
        s = summarize(d)
        return s["total"] if s.get("recorded") else None
    except Exception:
        return None


def list_all_projects() -> List[Dict[str, Any]]:
    """扫描 output/ 目录下所有有效工作项"""
    projects = []
    for d in sorted(DATA_ROOT.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if d.is_dir() and not d.name.startswith("."):
            prof_file = d / "profile.json"
            meta = {}
            if prof_file.exists():
                try:
                    with open(prof_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass
            book_info = meta.get("book", {})
            total = 0
            for pj in d.glob("Chapter_*/problems.json"):
                try:
                    total += len(json.loads(pj.read_text(encoding="utf-8")))
                except Exception:
                    pass
            projects.append({
                "id": d.name,                      # 即 output/ 下的文件夹名
                "name": d.name,
                "path": str(d),
                "title": book_info.get("title", d.name),
                "doc_type": book_info.get("doc_type", "exam"),
                "subject": book_info.get("subject", "general"),
                "has_pdf": len(list(d.glob("*.pdf"))) > 0,
                "total": total,
                "solved": len(list(d.glob("Chapter_*/slots/slot_*.json"))),
                "has_book_pdf": (d / "Book_All.pdf").exists(),
                "tokens": _usage_total(d),
            })
    return projects

def get_active_project_dir(project_name: Optional[str] = None) -> Path:
    """获取当前正在操作的活跃工程目录"""
    if project_name:
        p = DATA_ROOT / project_name
        p.mkdir(parents=True, exist_ok=True)
        return p

    env_p = os.environ.get("STUDYHELP_PROJECT")
    if env_p:
        p = DATA_ROOT / env_p
        p.mkdir(parents=True, exist_ok=True)
        return p

    # 自动检索 data 目录下最新修改的项目
    projects = list_all_projects()
    if projects:
        return Path(projects[0]["path"])

    # 默认创建
    default_p = DATA_ROOT / "default"
    default_p.mkdir(parents=True, exist_ok=True)
    return default_p

def get_chapters_root(project_dir: Optional[Path] = None) -> Path:
    """为兼容既有章节路径调用，返回当前活跃工程的根目录"""
    if project_dir is None:
        return get_active_project_dir()
    return Path(project_dir)

# 动态导出 CHAPTERS_ROOT（指向当前活跃项目目录）
CHAPTERS_ROOT = get_active_project_dir()
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "profile.json"

def load_book_config(project_dir: Optional[Path] = None) -> Dict[str, Any]:
    """读取指定项目（或默认活跃项目）的 profile.json"""
    p_dir = project_dir or get_active_project_dir()
    prof_file = p_dir / "profile.json"
    if prof_file.exists():
        with open(prof_file, "r", encoding="utf-8") as f:
            return json.load(f)
            
    # 备用兼容
    alt_file = p_dir / "book_profile.json"
    if alt_file.exists():
        with open(alt_file, "r", encoding="utf-8") as f:
            return json.load(f)

    return {
        "book": {
            "id": p_dir.name,
            "title": p_dir.name,
            "doc_type": "exam",
            "subject": "math",
            "dpi": 150
        },
        "chapters": {}
    }

def find_pdf_path(project_dir: Optional[Path] = None) -> Path:
    """在当前项目目录内检索原始输入 PDF（排除已生成的 Book_*.pdf 与 Chapter_*.pdf）"""
    p_dir = project_dir or get_active_project_dir()
    
    candidates = []
    for pdf_f in p_dir.glob("*.pdf"):
        name = pdf_f.name
        if not (name.startswith("Book_") or name.startswith("Chapter_") or name.startswith("_temp")):
            candidates.append(pdf_f)
            
    if candidates:
        return candidates[0].resolve()

    # 如果只有生成的 PDF，返回第一个可用的 PDF
    all_pdfs = list(p_dir.glob("*.pdf"))
    if all_pdfs:
        return all_pdfs[0].resolve()

    raise FileNotFoundError(f"项目目录 [{p_dir.name}] 下未找到任何 PDF 文件，请将教材/试卷 PDF 放入该目录。")

def get_book_metadata(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if cfg is None:
        cfg = load_book_config()
    b = cfg.get("book", {})
    return {
        "id": b.get("id", "study_help_doc"),
        "title": b.get("title", "StudyHelp 交互题库"),
        "subtitle": b.get("subtitle", "通用智能题解与分步推导"),
        "author": b.get("author", "StudyHelp 命题组"),
        "doc_type": b.get("doc_type", "exam"),
        "subject": b.get("subject", "general"),
        "dpi": b.get("dpi", 150)
    }

def get_all_chapters(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if cfg is None:
        cfg = load_book_config()
    return cfg.get("chapters", {})

def get_chapter_config(chapter_id: Any, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if cfg is None:
        cfg = load_book_config()
    chapters = cfg.get("chapters", {})
    ch_key = str(chapter_id)
    if ch_key in chapters:
        return chapters[ch_key]
    raise ValueError(f"章节/大题 [{chapter_id}] 未在配置中定义。已定义章节: {list(chapters.keys())}")
