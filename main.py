# -*- coding: utf-8 -*-
"""
StudyHelp 统一主入口 (StudyHelp Unified Master Entry)
根目录唯一 Python 脚本，统一驱动 Web 交互工作台与 CLI 流水线。

使用示例：
  python main.py                     # 启动 Web 交互工作台 (默认端口 8089 并自动弹窗浏览器)
  python main.py serve --port 8089   # 指定端口启动 Web 工作台
  python main.py check-env           # 检查当前工程环境与文档状态
  python main.py run-all             # 一键运行全流程 (切片 -> 聚合 -> 编译 PDF)
  python main.py ingest --pdf "..."  # 导入新教材/试卷 PDF 并初始化工程
  python main.py slice               # 执行全真紧凑无缝切片
  python main.py solve               # 执行 AI 多阶段推导
  python main.py compile-pdf         # 编译出版级三大 PDF
"""

import sys
import os
import argparse
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser(
        description="StudyHelp 通用教材与试卷智能题库系统",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # 1. 注册 Web 服务器子命令
    p_serve = subparsers.add_parser("serve", help="启动 Web 交互工作台")
    p_serve.add_argument("--port", type=int, default=8089, help="服务端口 (默认 8089)")
    p_serve.add_argument("--project", help="指定启动的项目名称")

    # 2. 注册流水线 CLI 子命令
    from core.pipeline import setup_cli_parser
    setup_cli_parser(subparsers)

    # 如果无参数传入，默认启动 Web 交互工作台
    if len(sys.argv) == 1:
        import webbrowser
        port = 8089
        url = f"http://127.0.0.1:{port}/review"
        print(f"[*] 默认启动 StudyHelp 交互工作台: {url}")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        from core.server import start_server
        start_server(port=port)
        return

    args = parser.parse_args()

    if args.command == "serve":
        if getattr(args, "project", None):
            os.environ["STUDYHELP_PROJECT"] = args.project
        from core.server import start_server
        start_server(port=args.port)
    elif hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
