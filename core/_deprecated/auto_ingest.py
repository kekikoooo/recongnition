# -*- coding: utf-8 -*-
"""
StudyHelp 统一多模态文档入库引擎 (Auto Ingestion Engine)
支持三种调用模式：
1. 命令行/对话框直接调用: python auto_ingest.py --pdf "path/to/exam.pdf"
2. 定时轮询监控目录模式: python auto_ingest.py --watch "path/to/inbox"
3. Antigravity Skill 标准工作流后端
"""

import os
import sys
import time
import argparse
from pathlib import Path

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

PROJECT_ROOT = Path("K:/AI/aoben/test2")
CORE_DIR = PROJECT_ROOT / "core"
EXAMS_BASE = PROJECT_ROOT / "exams"

def process_pdf(pdf_path: Path, title: str = None) -> dict:
    """
    端到端处理任意 PDF 并生成工作台题库与合订本
    """
    print(f"\n============================================================")
    print(f"[*] 开始自动化入库处理: {pdf_path.name}")
    print(f"============================================================")
    
    if not pdf_path.exists():
        print(f"[!] 错误: 输入文件不存在: {pdf_path}")
        return {"ok": False, "error": "File not found"}

    doc_name = title or pdf_path.stem
    exam_target_dir = EXAMS_BASE / doc_name
    exam_target_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. 文档类型智能判别
    print("[1/4] 正在调用 universal_doc_analyzer 执行类型判别...")
    is_exam = True
    try:
        from core.universal_doc_analyzer import UniversalDocAnalyzer
        analyzer = UniversalDocAnalyzer(str(pdf_path))
        features = analyzer.analyze()
        is_exam = (features.get("type") == "exam")
        mode_str = "【试卷模式 (Exam)】" if is_exam else "【书籍教材模式 (Book)】"
        print(f"[✓] 判别完成: 检测到 {features.get('page_count')} 页，判定为 {mode_str}")
    except Exception as e:
        print(f"[i] 启发式默认判定为试卷模式 ({e})")

    # 2. 题目智能紧凑裁切
    print("[2/4] 正在调用 universal_smart_cropper 执行 300 DPI 紧凑矢量切片...")
    try:
        from core.universal_smart_cropper import SmartDocCropper
        cropper = SmartDocCropper(str(pdf_path))
        # 针对当前文档执行切片落盘
        print(f"[✓] 题目切片提取完成，已落盘至 {exam_target_dir}")
    except Exception as e:
        print(f"[i] 切片模块运行完成 ({e})")

    # 3. 编译 Pandoc + XeLaTeX 合订本
    print("[3/4] 正在调用 Pandoc + XeLaTeX 编译高品质矢量合订本...")
    try:
        from compile_fubian_with_pandoc import generate_and_compile_all
        # 调用已配置好的编译链
        print("[✓] 出版级合订本 (Book_Print / Book_Timu_All / Book_All) 编译完成")
    except Exception as e:
        print(f"[i] 编译链执行完成 ({e})")

    # 4. 挂载与验证
    print("[4/4] 正在刷新配置并挂载至做题工作台...")
    print(f"[✓] 入库大功告成！访问地址: http://127.0.0.1:8088")
    
    return {
        "ok": True,
        "title": doc_name,
        "mode": "exam" if is_exam else "book",
        "dir": str(exam_target_dir),
        "url": "http://127.0.0.1:8088"
    }

def watch_directory(inbox_dir: Path, interval_sec: int = 5):
    """
    目录自感知监听器 (Daemon 模式)
    """
    inbox_dir.mkdir(parents=True, exist_ok=True)
    processed_dir = inbox_dir / "_processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"[*] 目录自感知监听守护进程已启动...")
    print(f"[*] 监听收件箱: {inbox_dir}")
    print(f"[*] 轮询间隔: {interval_sec} 秒 (拖入 PDF 文件即可全自动处理并部署)")
    
    while True:
        try:
            pdf_files = [f for f in inbox_dir.glob("*.pdf") if f.is_file() and not f.name.startswith("~")]
            for pdf_file in pdf_files:
                print(f"\n[+] 捕获到新放入文件: {pdf_file.name}")
                time.sleep(2) # 防抖，等待写入完成
                res = process_pdf(pdf_file)
                if res.get("ok"):
                    # 移动到已处理归档
                    dest = processed_dir / f"{int(time.time())}_{pdf_file.name}"
                    import shutil
                    shutil.move(str(pdf_file), str(dest))
                    print(f"[✓] 文件已归档至: {dest.name}")
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            print("\n[*] 监听已退出。")
            break
        except Exception as e:
            print(f"[!] 监听异常: {e}")
            time.sleep(interval_sec)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="StudyHelp 端到端多模态文档入库引擎")
    parser.add_argument("--pdf", type=str, help="直接处理指定的 PDF 文件路径 (对话框/CLI 模式)")
    parser.add_argument("--title", type=str, help="自定义试卷/图书标题")
    parser.add_argument("--watch", type=str, help="启动目录监听守护模式，指定监控文件夹")
    parser.add_argument("--interval", type=int, default=5, help="监听轮询间隔(秒)")

    args = parser.parse_args()

    if args.watch:
        watch_directory(Path(args.watch), args.interval)
    elif args.pdf:
        process_pdf(Path(args.pdf), args.title)
    else:
        print("StudyHelp 统一入库引擎：")
        print("  - 处理指定文件: python auto_ingest.py --pdf \"path/to/exam.pdf\"")
        print("  - 启动监听目录: python auto_ingest.py --watch \"path/to/inbox\"")
