# -*- coding: utf-8 -*-
"""
StudyHelp 出版级 PDF 编译引擎 (Universal PDF Compiler - 生产级规范版)
使用 Pandoc + XeLaTeX + 典藏级宏包配置，自动编译全书 3 大核心出版级 PDF
1. Book_Print.pdf: 便携省纸高密度速查合订本
2. Book_All.pdf: 期末全真满分题解大合订本 (完整分步推导)
3. Book_Timu_All.pdf: 原卷全宽高清书影题集 (纯净真题切片，杜绝浮动图表与答案剧透)
"""

import os
import sys
import re
import time
import subprocess
from pathlib import Path
from typing import Optional, Dict

from core.config import CHAPTERS_ROOT, load_book_config, get_book_metadata

PANDOC_EXE = Path("K:/AI/VSCode/pandoc/pandoc.exe")
LUA_FILTER = Path("K:/AI/VSCode/pandoc/k-ai-vscode-pandoc/.vscode/remove-horizontal-rules.lua")
TEX_HEADER = Path("K:/AI/VSCode/pandoc/k-ai-vscode-pandoc/.vscode/pandoc-pdf-header.tex")

EMOJI_PATTERN = re.compile(r'[\U00010000-\U0010ffff]')
TAG_PATTERN = re.compile(r'\\tag\{([^}]+)\}')

def sanitize_markdown_for_xelatex(md_text: str, is_timu: bool = False) -> str:
    """全面清洗 Markdown 源码，确保 100% 免疫 XeLaTeX 报错与排版溢出"""
    if not md_text:
        return ""

    # 1. 剔除所有 Emoji 图标
    text = EMOJI_PATTERN.sub('', md_text)
    text = text.replace("💡", "").replace("⚡", "").replace("🎯", "").replace("📝", "")
    text = text.replace("📖", "").replace("📘", "").replace("🖨️", "").replace("🔍", "").replace("📌", "")

    # 2. 规范化 LaTeX 标点
    text = TAG_PATTERN.sub(r'\\quad \\text{(\1)}', text)
    # 将 $$ 后面紧贴的标点移到外部换行
    # 只在公式内部找（[^$]，不跨 $）：原来的 [\s\S]+? 会一路扫到文件末尾，全书（2MB+）上是平方级慢，清洗要好几分钟
    text = re.sub(r'\$\$([^$]+?)\$\$([，。、；,.;!?])', r'$$\n\1\n$$\n\2\n', text)

    # 2b. 行内公式 `$ x $` 内侧有空格时 pandoc 不认作公式，里面的 _ ^ 会让 LaTeX 报 Missing $，统一去掉内侧空格
    text = re.sub(r'(?<![$\\])\$(?!\$)([^$\n]+?)(?<!\\)\$(?!\$)',
                  lambda m: "$" + m.group(1).strip() + "$", text)

    # 2c. 上/下标后面直接跟 \mathbb{R} 之类（没有花括号包住）时，unicode-math 会报 Missing { inserted，整本 PDF 失败；统一补上花括号
    text = re.sub(r"([\^_])\\(math(?:bb|cal|bf|rm|it|frak|sf|tt|scr|bfit))\{([^{}]*)\}", r"\1{\\\2{\3}}", text)

    # 2d. centernot 命令需要 centernot 宏包，没装就是 Undefined control sequence；换成 not（效果一样，画一条斜线）
    text = text.replace("\\centernot", "\\not")
    # 2d'. unicode-math 下 \not 后面跟 \implies / \Rightarrow 这类会报 Missing { inserted；改用现成的“否定箭头”符号
    for _a, _b in (("\\not\\implies", "\\nRightarrow"), ("\\not\\Rightarrow", "\\nRightarrow"),
                   ("\\not\\Longrightarrow", "\\nRightarrow"), ("\\not\\impliedby", "\\nLeftarrow"),
                   ("\\not\\Leftarrow", "\\nLeftarrow"), ("\\not\\iff", "\\nLeftrightarrow"),
                   ("\\not\\Leftrightarrow", "\\nLeftrightarrow"), ("\\not\\rightarrow", "\\nrightarrow"),
                   ("\\not\\to", "\\nrightarrow"), ("\\not\\mid", "\\nmid"), ("\\not\\in", "\\notin")):
        text = text.replace(_a, _b)
    # 2e. 显示公式 $$…$ 开头两个美元符号、结尾只有一个（或反过来）会报 Display math should end with $$；逐行补齐
    def _fix_dollar(line):
        t = line.rstrip()
        if t.startswith('$$') and t.endswith('$') and not t.endswith('$$') and t.count('$') % 2 == 1:
            return t + '$'
        if t.startswith('$') and not t.startswith('$$') and t.endswith('$$') and t.count('$') % 2 == 1:
            return t[:-1]
        return line
    text = "\n".join(_fix_dollar(l) for l in text.split("\n"))

    # 3. 彻底清空图片 alt text，强制生成原生内联图形，根除 LaTeX figure 浮动与丑陋的 Figure 标号
    text = re.sub(r'!\[.*?\]\((.*?)\)', r'![](\1){width=95%}', text)

    # 4. 优化分页符
    text = text.replace('<div style="page-break-after: always;"></div>', '\n\\newpage\n')

    return text

def compile_markdown_to_pdf(input_md: Path, output_pdf: Path, resource_dir: Path, is_timu: bool = False, quiet: bool = False) -> bool:
    input_md, output_pdf, resource_dir = Path(input_md).resolve(), Path(output_pdf).resolve(), Path(resource_dir).resolve()
    if not PANDOC_EXE.exists():
        print(f"[FAIL] Pandoc 可执行文件不存在: {PANDOC_EXE}")
        return False

    if not input_md.exists():
        print(f"[FAIL] 输入 Markdown 文件不存在: {input_md}")
        return False

    if not quiet:
        print(f"--> [Pandoc + XeLaTeX] 正在编译 {output_pdf.name} ...", flush=True)

    # 生成临时的清洗后 Markdown 文件
    raw_content = input_md.read_text(encoding="utf-8")
    sanitized_content = sanitize_markdown_for_xelatex(raw_content, is_timu=is_timu)
    temp_clean_md = input_md.parent / f"_temp_clean_{input_md.name}"
    temp_clean_md.write_text(sanitized_content, encoding="utf-8")

    cmd = [
        str(PANDOC_EXE),
        str(temp_clean_md),
        "--from=markdown-yaml_metadata_block+lists_without_preceding_blankline-fancy_lists+raw_tex",  # 正文中的 --- 不能被当成 YAML 头
        "--pdf-engine=xelatex",
        "--pdf-engine-opt=-interaction=nonstopmode",
        f"--lua-filter={LUA_FILTER}" if LUA_FILTER.exists() else "",
        f"--include-in-header={TEX_HEADER}" if TEX_HEADER.exists() else "",
        f"--resource-path={resource_dir}",
        "-V", "mainfont=Times New Roman",
        "-V", "monofont=Consolas",
        "-V", "geometry:margin=1.5cm",
        "-o", str(output_pdf)
    ]
    # 过滤空参数
    cmd = [c for c in cmd if c]

    env = os.environ.copy()
    env["LANG"] = "en_US.UTF-8"
    env["PYTHONIOENCODING"] = "utf-8"

    t0 = time.time()
    res = subprocess.run(
        cmd,
        cwd=str(resource_dir),
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace'
    )
    elapsed = time.time() - t0

    # 清理临时文件
    if temp_clean_md.exists():
        try:
            temp_clean_md.unlink()
        except Exception:
            pass

    if res.returncode == 0 and output_pdf.exists():
        size_kb = output_pdf.stat().st_size / 1024
        if not quiet:
            print(f"[OK] 编译出版级 PDF 成功: {output_pdf.name} ({size_kb:.1f} KB, 耗时 {elapsed:.1f}s)", flush=True)
        return True
    else:
        if not quiet:
            print(f"[FAIL] 编译失败: {output_pdf.name} (code={res.returncode})", flush=True)
        if res.stderr and not quiet:
            err_lines = [l for l in res.stderr.split('\n') if l.startswith('!') or 'Error' in l or 'Fatal' in l]
            print("错误摘要:\n" + "\n".join(err_lines[-10:]), flush=True)
        return False

_SECTION_RE = re.compile(r'^(?=(?:### 第 |## 习题 ))', re.MULTILINE)   # 全书/速查用“### 第 X 题”，打印版用“## 习题 X”


def _degrade_section(sec: str) -> str:
    """公式无法排版的题：保留标题，正文改为纯文本代码块，并提示查看网页版。"""
    head, _, body = sec.partition("\n")
    body = body.replace("```", "'''")
    return (f"{head}\n\n> 【排版降级】本题部分公式无法在 PDF 中排版，以下为原始文本，完整渲染请看网页版。\n\n"
            f"```text\n{body.strip()}\n```\n\n")


_CUR = [0.0, 0.0]        # 当前文件在整体里的起点和占比（由 compile_all_suite_pdfs 设置）
PDF_WORKERS = 3          # 同时编译的文件数：实测 7 个一起编，每个慢 10 倍（xelatex 抢字体缓存和磁盘），总时间反而更长
PROGRESS = None          # 进度回报：PROGRESS(消息, 百分比)；由构建任务设置，让进度条在 PDF 阶段也会动
_PCT = [92.0, 99.0]      # PDF 阶段占总进度的 92%~99%


def _report(msg: str, frac: float):
    cb = PROGRESS
    if cb:
        try:
            cb(msg, int(_PCT[0] + (_PCT[1] - _PCT[0]) * max(0.0, min(1.0, frac))))
        except Exception:
            pass


_PROBE_CACHE: Dict[str, bool] = {}     # 题段内容 -> 单独编译是否通过（同一题在各章打印版、全书版里会反复出现，只试一次）


def _probe_sections(secs: list, workdir: Path, resource_dir: Path, workers: int = 6) -> list:
    """每道题单独编译一次（多进程并行），一遍找出所有公式不合法的题。
    比二分快且可靠：二分在“多个坏题互相牵连”时会反复重编整本，动辄几十分钟。"""
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    todo = {}
    for i, sec in enumerate(secs):
        key = hashlib.md5(sec.encode("utf-8")).hexdigest()
        if key not in _PROBE_CACHE:
            todo[key] = i

    done_n = [0]
    _label = workdir.name

    def probe(item):
        key, i = item
        md = workdir / f"_probe_{os.getpid()}_{i}.md"
        pdf = workdir / f"_probe_{os.getpid()}_{i}.pdf"
        try:
            md.write_text(secs[i], encoding="utf-8")
            _PROBE_CACHE[key] = compile_markdown_to_pdf(md, pdf, resource_dir, quiet=True)
            done_n[0] += 1
            _report(f"编译 PDF：公式检查 {done_n[0]}/{len(todo)}（找出无法排版的题）", _CUR[0] + _CUR[1] * 0.8 * done_n[0] / max(1, len(todo)))
        finally:
            for fx in (md, pdf):
                if fx.exists():
                    try:
                        fx.unlink()
                    except Exception:
                        pass

    if todo:
        print(f"[*] 逐题单独试编译 {len(todo)} 道（{workers} 路并行），找出公式不合法的题…", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(probe, todo.items()))
    return [i for i, sec in enumerate(secs) if not _PROBE_CACHE.get(hashlib.md5(sec.encode("utf-8")).hexdigest(), True)]


def compile_markdown_resilient(input_md: Path, output_pdf: Path, resource_dir: Path) -> bool:
    """先整本编译；失败则逐题并行试编译，找出公式不合法的题，降级为纯文本后再整本编译。
    模型生成的上百道题里只要一个公式不合法，整本 PDF 就会失败，所以必须有这一层。"""
    if compile_markdown_to_pdf(input_md, output_pdf, resource_dir):
        return True
    text = input_md.read_text(encoding="utf-8")
    parts = _SECTION_RE.split(text)
    head, secs = parts[0], parts[1:]
    if not secs:
        return False
    bad = _probe_sections(secs, input_md.parent, resource_dir)
    if not bad:
        return False
    names = [secs[i].split(chr(10), 1)[0].strip("# ").strip() for i in bad]
    print(f"[*] 降级为纯文本的题（{len(bad)} 道）: {names}", flush=True)
    for i in bad:
        secs[i] = _degrade_section(secs[i])
    fixed_md = input_md.parent / f"_fixed_{input_md.name}"
    fixed_md.write_text(head + "".join(secs), encoding="utf-8")
    try:
        return compile_markdown_to_pdf(fixed_md, output_pdf, resource_dir)
    finally:
        if fixed_md.exists():
            fixed_md.unlink()


def compile_all_suite_pdfs(project_dir: Optional[Path] = None) -> Dict[str, bool]:
    """一键编译全书全部 3 套高品质 PDF 合订本（可指定项目目录，默认当前活跃项目）"""
    from core.config import get_active_project_dir
    CHAPTERS_ROOT = Path(project_dir) if project_dir else get_active_project_dir()
    cfg = load_book_config(CHAPTERS_ROOT)
    meta = get_book_metadata(cfg)

    print("=" * 60)
    print(f"StudyHelp 出版级 PDF 编译引擎 · 启动")
    print(f"文献/书目: 《{meta['title']}》")
    print("=" * 60)

    results = {}
    
    # 任务清单：每章打印版（网页顶部“打印.pdf”按钮用）+ 全书三套；权重按大致耗时
    tasks = []
    for cd in sorted(CHAPTERS_ROOT.glob("Chapter_*")):
        pm = cd / f"{cd.name}_Print.md"
        if cd.is_dir() and pm.exists():
            tasks.append((f"第 {int(cd.name.split('_')[1])} 章打印版", pm, cd / f"{cd.name}_Print.pdf", cd, "resilient", 1.0))
    if (CHAPTERS_ROOT / "Book_Print.md").exists():
        tasks.append(("全书打印版", CHAPTERS_ROOT / "Book_Print.md", CHAPTERS_ROOT / "Book_Print.pdf", CHAPTERS_ROOT, "resilient", 1.5))
    if (CHAPTERS_ROOT / "Book_All.md").exists():
        tasks.append(("全书详解版", CHAPTERS_ROOT / "Book_All.md", CHAPTERS_ROOT / "Book_All.pdf", CHAPTERS_ROOT, "resilient", 3.0))
    if (CHAPTERS_ROOT / "Book_Timu_All.md").exists():
        tasks.append(("习题全书（含原图）", CHAPTERS_ROOT / "Book_Timu_All.md", CHAPTERS_ROOT / "Book_Timu_All.pdf", CHAPTERS_ROOT, "timu", 1.5))
    # 各文件互不依赖：同时编译（xelatex 单个文件内部没法并行，但多个文件可以）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # 先编全书三套（网页“全书合订本”弹窗里那三个），最耗时的先开始；每章打印版排在后面
    tasks.sort(key=lambda t: (0, -t[5]) if t[3] == CHAPTERS_ROOT else (1, 0))
    total_w = sum(t[5] for t in tasks) or 1.0
    done_w = [0.0]
    _CUR[0], _CUR[1] = 0.0, 0.0          # 并行时不再按单个文件细分进度，以“完成了几个文件”为准
    _report(f"编译 PDF：{len(tasks)} 个文件同时编译…", 0.0)

    def run_one(t):
        label, md, pdf, res_dir, kind, w = t
        if kind == "timu":
            return t, compile_markdown_to_pdf(md, pdf, res_dir, is_timu=True)
        return t, compile_markdown_resilient(md, pdf, res_dir)

    finished = 0
    with ThreadPoolExecutor(max_workers=max(1, min(PDF_WORKERS, len(tasks)))) as ex:
        for fut in as_completed([ex.submit(run_one, t) for t in tasks]):
            t, ok_ = fut.result()
            results[t[2].name] = ok_
            finished += 1
            done_w[0] += t[5]
            _report(f"编译 PDF：{t[0]}{'完成' if ok_ else '失败'}（{finished}/{len(tasks)}）", done_w[0] / total_w)

    print("=" * 60)
    print("PDF 出版级编译验收结果:")
    for k, v in results.items():
        status = "[OK] 成功" if v else "[FAIL] 失败"
        print(f" - {k:<20}: {status}")
    print("=" * 60)

    return results

if __name__ == "__main__":
    compile_all_suite_pdfs()
