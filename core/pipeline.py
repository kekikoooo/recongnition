# -*- coding: utf-8 -*-
"""
StudyHelp 通用题库构建与控制总线流水线 (Universal Pipeline)
支持任意高校试卷、经典教材课后习题的一键端到端处理
用法示例：
  python main.py check-env
  python main.py ingest --pdf "path/to/doc.pdf"
  python main.py slice
  python main.py solve
  python main.py aggregate
  python main.py compile-pdf
  python main.py run-all
"""

import sys
import argparse
from pathlib import Path

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from core.config import (
    load_book_config, get_book_metadata, get_all_chapters,
    find_pdf_path, CHAPTERS_ROOT, get_active_project_dir, list_all_projects, DATA_ROOT
)
from core.cropper import UniversalCropper
from core.solver import solve_chapter
from core.aggregator import aggregate_chapter, aggregate_all_book
from core.pdf_compiler import compile_all_suite_pdfs

def cmd_check_env(args):
    print("=" * 60)
    print("StudyHelp 运行环境与工程配置自检")
    print("=" * 60)
    p_dir = get_active_project_dir(getattr(args, "project", None))
    print(f"[OK] 当前活跃工程: {p_dir.name}")
    print(f"[OK] 数据存放路径: {p_dir}")

    cfg = load_book_config(p_dir)
    meta = get_book_metadata(cfg)
    print(f"[OK] 题库/试卷全称: {meta['title']}")
    print(f"[OK] 文档类型: {meta['doc_type']} | 学科: {meta['subject']}")
    
    try:
        pdf_path = find_pdf_path(p_dir)
        print(f"[OK] 已定位原始 PDF: {pdf_path.name}")
    except Exception as e:
        print(f"[FAIL] 未找到 PDF 文件: {e}")

    chapters = get_all_chapters(cfg)
    print(f"[OK] 当前配置大题/章节数: {len(chapters)} 个")
    for k, v in chapters.items():
        print(f"    - [{k}] {v.get('name', '')} (页码: {v.get('start_page')}-{v.get('end_page')})")
    print("=" * 60)

def cmd_ingest(args):
    """PDF -> output/<书名>/ -> 切题 -> 求解(带评审闸门) -> 汇编 MD -> 编译 PDF（与网页上传同一条流水线）"""
    import json
    import shutil
    from core.ingest_job import run_ingest, INGEST
    pdf_file = Path(args.pdf)
    if not pdf_file.exists():
        print(f"[FAIL] 输入 PDF 不存在: {pdf_file}")
        return
    doc_name = (getattr(args, "title", None) or pdf_file.stem).strip()
    proj_dir = DATA_ROOT / doc_name
    proj_dir.mkdir(parents=True, exist_ok=True)
    target_pdf = proj_dir / pdf_file.name
    if not target_pdf.exists() or target_pdf.resolve() != pdf_file.resolve():
        shutil.copy2(str(pdf_file), str(target_pdf))

    doc_type = getattr(args, "type", "book")
    subject = getattr(args, "subject", "general")
    profile = None
    try:
        from core.auto_profiler import auto_generate_and_save_profile
        profile = auto_generate_and_save_profile(str(target_pdf), out_profile_path=proj_dir / "profile.json",
                                                 doc_type=doc_type, subject=subject, title=doc_name)
    except Exception as e:
        print(f"[!] 自动嗅探 profile 失败，使用最小 profile: {e}")
    if profile is None:
        profile = {"book": {"id": doc_name, "title": doc_name, "doc_type": doc_type, "subject": subject, "dpi": 150},
                   "chapters": {}}
        (proj_dir / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f">>> 类型: {doc_type} | 学科: {subject} | 工程目录: {proj_dir}")
    run_ingest(proj_dir, solve=not getattr(args, "no_solve", False),
               concurrency=getattr(args, "concurrency", 4))
    if INGEST.get("stage") == "error":
        print(f"[FAIL] {INGEST.get('error')}")
    else:
        print(f"[OK] 全流程完成，工程目录: {proj_dir}")

def cmd_slice(args):
    p_dir = get_active_project_dir(getattr(args, "project", None))
    pdf_p = find_pdf_path(p_dir)
    print(f">>> 启动全宽紧凑自适应物理切片引擎: {pdf_p.name} ...")
    cropper = UniversalCropper(str(pdf_p))
    results = cropper.crop_all()
    total_probs = sum(len(v) for v in results.values())
    print(f"[OK] 全书切片完成，累计切出 {total_probs} 道纯净题目切片！")

def cmd_solve(args):
    p_dir = get_active_project_dir(getattr(args, "project", None))
    cfg = load_book_config(p_dir)
    chapters = get_all_chapters(cfg)
    
    target_chapters = [args.chapter] if args.chapter else list(chapters.keys())
    print(f">>> 启动多阶段高精度解题引擎，目标大题/章节: {target_chapters}")
    
    for ch_key in target_chapters:
        ch_idx = int(ch_key) if str(ch_key).isdigit() else 1
        print(f"--> 开始求解第 {ch_key} 部分...")
        solve_chapter(ch_idx, concurrency=args.concurrency, cfg=cfg)
        aggregate_chapter(ch_key, cfg=cfg)

    print(f">>> 正在汇总全书合订本...")
    aggregate_all_book(cfg)
    print(f"[OK] 求解与聚合完毕！")

def cmd_aggregate(args):
    p_dir = get_active_project_dir(getattr(args, "project", None))
    cfg = load_book_config(p_dir)
    print(f">>> 正在聚合汇编工程 [{p_dir.name}] 核心三大合订本...")
    aggregate_all_book(cfg)
    print(f"[OK] Markdown 合订本聚合完毕！")

def cmd_compile_pdf(args):
    p_dir = get_active_project_dir(getattr(args, "project", None))
    cfg = load_book_config(p_dir)
    print(f">>> 正在编译工程 [{p_dir.name}] 出版级三大 PDF...")
    aggregate_all_book(cfg)
    compile_all_suite_pdfs()
    print(f"[OK] 出版级 PDF 编译完成！")

def cmd_run_all(args):
    from core.ingest_job import run_ingest, INGEST
    p_dir = get_active_project_dir(getattr(args, "project", None))
    print(f"StudyHelp 端到端全流程 · 目标工程: {p_dir.name}")
    run_ingest(p_dir, solve=not getattr(args, "no_solve", False), concurrency=getattr(args, "concurrency", 4))
    print(f"[{'FAIL' if INGEST.get('stage') == 'error' else 'OK'}] {INGEST.get('message')}  成果目录: {p_dir}")

def setup_cli_parser(subparsers):
    p_check = subparsers.add_parser("check-env", help="检查运行环境与文档配置")
    p_check.add_argument("--project", help="指定项目名称")
    p_check.set_defaults(func=cmd_check_env)

    p_ingest = subparsers.add_parser("ingest", help="全自动智能嗅探与题库入库")
    p_ingest.add_argument("--type", choices=["book", "exam"], default="book", help="教材(book)或试卷(exam)")
    p_ingest.add_argument("--subject", default="general", help="学科: math/ee/physics/general")
    p_ingest.add_argument("--title", help="工程名称")
    p_ingest.add_argument("--concurrency", type=int, default=4)
    p_ingest.add_argument("--no-solve", action="store_true", help="只切题不解题")
    p_ingest.add_argument("--pdf", required=True, help="输入 PDF 文件绝对路径")
    p_ingest.set_defaults(func=cmd_ingest)

    p_slice = subparsers.add_parser("slice", help="执行全宽高清自适应物理切片")
    p_slice.add_argument("--project", help="指定项目名称")
    p_slice.set_defaults(func=cmd_slice)

    p_solve = subparsers.add_parser("solve", help="执行多阶段高精度解题")
    p_solve.add_argument("--project", help="指定项目名称")
    p_solve.add_argument("--chapter", help="指定大题/章节编号")
    p_solve.add_argument("--concurrency", type=int, default=4, help="并发数量")
    p_solve.set_defaults(func=cmd_solve)

    p_agg = subparsers.add_parser("aggregate", help="汇编生成三大 Markdown 合订本")
    p_agg.add_argument("--project", help="指定项目名称")
    p_agg.set_defaults(func=cmd_aggregate)

    p_pdf = subparsers.add_parser("compile-pdf", help="编译全套出版级 PDF")
    p_pdf.add_argument("--project", help="指定项目名称")
    p_pdf.set_defaults(func=cmd_compile_pdf)

    p_all = subparsers.add_parser("run-all", help="一键端到端运行全流程")
    p_all.add_argument("--concurrency", type=int, default=4)
    p_all.add_argument("--no-solve", action="store_true", help="只切题不解题")
    p_all.add_argument("--project", help="指定项目名称")
    p_all.set_defaults(func=cmd_run_all)
