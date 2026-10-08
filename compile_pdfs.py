# -*- coding: utf-8 -*-
"""
命令行直接编译一个工作项的整套 PDF（每章打印版 + 全书打印版 + 全书详解版 + 习题全书），不经过网页服务。
用法：python compile_pdfs.py [工作项名]      不写工作项名就用 output 里最近修改的那个
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from core.config import DATA_ROOT          # noqa: E402
from core import pdf_compiler              # noqa: E402


def main():
    if len(sys.argv) > 1:
        proj = Path(DATA_ROOT) / sys.argv[1]
    else:
        proj = max((d for d in Path(DATA_ROOT).iterdir() if d.is_dir() and (d / "Book_All.md").exists()),
                   key=lambda d: (d / "Book_All.md").stat().st_mtime)
    print(f"工作项：{proj.name}", flush=True)
    t0 = time.time()
    pdf_compiler.PROGRESS = lambda msg, pct: print(f"[{time.time() - t0:5.0f}s] {pct}%  {msg}", flush=True)
    res = pdf_compiler.compile_all_suite_pdfs(proj)
    print(f"\n全部结束，用时 {time.time() - t0:.0f}s", flush=True)
    for k, v in res.items():
        print(f"  {'成功' if v else '失败'}  {k}")


if __name__ == "__main__":
    main()
