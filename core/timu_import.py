# -*- coding: utf-8 -*-
"""
导入外部题目文本：用户把 PDF 交给大模型对话框（网页版，不走 API、不计本系统 token），
得到“全部题目”的 Markdown，再导入这里，按题号写进每道题的题干。

- 切题（切片图、跨页拼接）仍由本系统完成；导入的题干直接作为原题栏、Compact 和解题使用的题干，
  不再做“看图转写 + 逐符号核对”，这部分 token 全部省掉；
- 题号按 `### 3.12`（书：章.题号）或 `### 2.3`（试卷：第几大题.题号）对齐，也接受 3-12、习题3.12、第3.12题；
- 报告：哪些题对上了、哪些切出来了但文本里没有、哪些文本里有但没切出来（可能是切题漏了）。
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

IMPORT_FILE = "timu_import.md"
REPORT_FILE = "timu_import_report.json"

PROMPT = """请把我上传的这份 PDF 里的【全部习题/试题】逐字转写成 Markdown。

先做一件事：翻一遍整份 PDF，找出习题所在的页，【第一行】先输出页码范围（按这份 PDF 文件的第几页数，从文件第 1 页数起，不是书上印的页码；每章习题连在一起就写一个范围，拿不准就写宽一点，写窄了会漏题）：
习题页：第1章 23-27；第2章 58-63
试卷整份都是题就写：习题页：全部。这一行整份只写一次，放在最开头。

然后逐题输出，要求：
1. 每道题单独一段，题目开头单独一行写题号标题：
   - 教材：### 章号.题号（如书上的“3-12”或“习题 3.12”写成 ### 3.12）；
   - 试卷：### 第几大题.小题号（如“二、填空题”的第 3 小题写成 ### 2.3；小题号用卷面上印的号）。
2. 标题下面是完整题干：全部条件、数值、全部小问 (a)(b)…/(1)(2)…、选择题的全部选项，题干里不要再写题号。
3. 公式用 LaTeX：行内 $...$，单独成行 $$...$$；下标、上标、希腊字母、≈ 与 = 等逐个照原文，不要“纠正”原文。
4. 排版要和原图一致：换行位置照原图；原图居中、单独成行的公式写成 $$...$$ 单独成行；原图同一行并排的小问、选项或式子仍写在同一行，用 &emsp;&emsp; 分隔；原图分行的小问每问单独一行。
5. 插图不用画，在图的位置写【见原图】即可。填空横线写成 \\_\\_\\_\\_\\_\\_。
6. 输出之前先自己检查一遍，发现问题就改好再输出（不要输出检查过程）：
   ① 题号是否连续，有没有漏题、重复；
   ② 每道题里的 $ 是否成对，每个公式是否写完整（不能写到一半就断）；
   ③ 题干里有没有多余的题号、解答或说明文字。
7. 全部题目输出完以后，单独另起最后一行，列出所有依赖图的题号：只要题目里有插图、波形图、电路图、几何图、表格，或者题干要看图才能完整理解（如“如图所示”“见图 P3.16”），这道题就算有图。格式：
   有图题号：3.16、3.22、3.34
   没有一道有图就写：有图题号：无。纯文字、纯公式的题不要列。
8. 只输出题目，不要解答、不要任何说明；正文例题不算习题。题目很多时可以分几次输出，我会说“继续”（每一次输出末尾都要带“有图题号”这一行；“习题页”只在第一次的开头写）。"""

_HEAD_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:习题|题目|题|第)?\s*(\d{1,2})\s*[.\-－—–·．]\s*(\d{1,3})(?!\d)"
    r"\s*(?:题)?\s*(?:\*\*)?\s*[.。:：、]?\s*(.*)$")


_FIGLINE_RE = re.compile(r"^\s*[*>\-\s]*有图题号\s*[*]*\s*[:：]\s*[*]*\s*(.*)$")
_PAGELINE_RE = re.compile(r"^\s*[*>\-\s]*习题页\s*[*]*\s*[:：]\s*[*]*\s*(.*)$")


def parse_page_ranges(text: str, total_pages: int = 0, pad: int = 1, only_chapters: set = None) -> set:
    """文本里“习题页：第1章 23-27；第2章 58-63”这样的行（可多次，取并集）-> PDF 页码集合（1 基，前后各多 pad 页）。
    only_chapters：只保留这些章的页（导入的文本只有前几章时，后面的章不扫、不做）。
    没有这一行、写“全部”、或页码超出文件页数（写错了）时返回空集 = 不限制，照常全书扫描。"""
    pages = set()
    for ln in (text or "").splitlines():
        m = _PAGELINE_RE.match(ln)
        if not m:
            continue
        body = m.group(1)
        if "全部" in body or "全书" in body:
            return set()
        for seg in re.split(r"[；;。]", body):
            mc = re.search(r"第\s*(\d+)\s*章", seg)
            if only_chapters and mc and int(mc.group(1)) not in only_chapters:
                continue
            seg = re.sub(r"第\s*\d+\s*章", " ", seg)
            for x, y in re.findall(r"(\d{1,4})\s*[-－—–~～至到]\s*(\d{1,4})", seg):
                x, y = int(x), int(y)
                if x > y:
                    x, y = y, x
                pages.update(range(x, y + 1))
            seg = re.sub(r"(\d{1,4})\s*[-－—–~～至到]\s*(\d{1,4})", " ", seg)
            pages.update(int(v) for v in re.findall(r"\d{1,4}", seg))
    if not pages:
        return set()
    if total_pages and max(pages) > total_pages:
        return set()
    out = set()
    for p in pages:
        for q in range(p - pad, p + pad + 1):
            if q >= 1 and (not total_pages or q <= total_pages):
                out.add(q)
    return out


def chapters_in_text(text: str) -> set:
    """导入文本里出现的章号（题号 3.12 里的 3）"""
    parsed, _ = parse_timu(text)
    return {int(k.split(".")[0]) for k in parsed}


def parse_fig_list(text: str) -> set:
    """文本里“有图题号：3.16、3.22”这样的行（可出现多次，取并集）-> 题号集合"""
    out = set()
    for ln in (text or "").replace("\r\n", "\n").split("\n"):
        m = _FIGLINE_RE.match(ln)
        if m:
            for a, b in re.findall(r"(\d{1,2})\s*[.\-－—–·．]\s*(\d{1,3})", m.group(1)):
                out.add(f"{int(a)}.{int(b)}")
    return out


def parse_timu(text: str) -> Tuple[Dict[str, str], List[str]]:
    """返回 ({题号: 题干}, 重复的题号)。优先认 Markdown 标题行；文本里没有标题行时才认行首题号。"""
    # “有图题号”清单行不是题干：先去掉，免得并进最后一道题
    # 对话框常在开头/结尾附一行“本文对应 Markdown 文件：[...](file:///...)”，也不是题干
    lines = [ln for ln in (text or "").replace("\r\n", "\n").split("\n")
             if not _FIGLINE_RE.match(ln) and not _PAGELINE_RE.match(ln) and not ln.lstrip().startswith("👉") and "file:///" not in ln]
    has_md = any(re.match(r"^\s*#{1,6}\s*", ln) and _HEAD_RE.match(ln) for ln in lines)
    out: Dict[str, List[str]] = {}
    dup: List[str] = []
    prev: Dict[str, List[str]] = {}
    cur = None
    for ln in lines:
        m = _HEAD_RE.match(ln)
        if m and (not has_md or re.match(r"^\s*#{1,6}\s*", ln)):
            pid = f"{int(m.group(1))}.{int(m.group(2))}"
            if pid in out:
                dup.append(pid)
                # 重复出现（对话框“继续”时常把上一段末尾那题再写一遍，前一份往往被截断）：先另存，最后取更完整的
                prev[pid] = out[pid]
            cur = pid
            out[cur] = [m.group(3)] if m.group(3).strip() else []
            continue
        if cur is not None:
            out[cur].append(ln)
    for pid, buf in prev.items():
        if len("\n".join(buf).strip()) > len("\n".join(out[pid]).strip()):
            out[pid] = buf
    res = {}
    for pid, buf in out.items():
        t = "\n".join(buf).strip()
        t = re.sub(r"\n{3,}", "\n\n", t)
        t = re.sub(r"\n-{3,}\s*$", "", t).strip()
        if t:
            res[pid] = t
    return res, dup


def is_damaged(t: str) -> bool:
    """题干里的公式明显没写完：去掉 \\$ 和 $$ 之后，剩下的 $ 个数为奇数"""
    s = (t or "").replace("\\$", "").replace("$$", "")
    return s.count("$") % 2 == 1


def apply_import(project_dir: Path, text: str, check: bool = False) -> Dict[str, Any]:
    """把导入的题干写进 problems.json 和已有解答的 slot，返回对齐报告。
    check=False：text_source=imported，直接信任，不再看图核对（省 token）；
    check=True ：text_source=imported_check，题干核对环节仍逐题对照原图核对（更稳，不省 token）。"""
    from core.solver import normalize_blanks
    p_dir = Path(project_dir)
    parsed, dup = parse_timu(text)
    fig_pids = parse_fig_list(text)
    fig_line = bool(re.search(r"有图题号\s*[:：]", text or ""))
    (p_dir / IMPORT_FILE).write_text(text, encoding="utf-8")
    damaged: List[str] = []
    cropped: List[str] = []
    matched: List[str] = []
    replaced_solved: List[str] = []
    for pj in sorted(p_dir.glob("Chapter_*/problems.json")):
        probs = json.loads(pj.read_text(encoding="utf-8"))
        for prob in probs:
            pid = str(prob["problem_id"])
            cropped.append(pid)
            if pid not in parsed:
                continue
            t = normalize_blanks(parsed[pid])
            if is_damaged(t):
                damaged.append(pid)      # 导入文本里这题公式断了：不导入，仍由系统按原图转写
                continue
            t = t.replace("【有图】", "").strip()
            prob["text"], prob["text_source"] = t, ("imported_check" if check else "imported")
            # 有图标记：决定解题时要不要把切片图一起发给模型（纯文字题只发文字，省 token；有图题文字+图）。
            # 文本里没有“有图题号”这一行（大模型漏写）时不敢省：记为 None，解题照常带图。
            if fig_line:
                prob["has_fig"] = (pid in fig_pids) or ("见原图" in t) or bool(re.search(r"如图|图\s*[A-Za-z]?\s*\d", t))
            else:
                prob["has_fig"] = None
            matched.append(pid)
            sf = pj.parent / "slots" / f"slot_{pid}.json"
            if sf.exists():
                s = json.loads(sf.read_text(encoding="utf-8"))
                if s.get("text") != t:
                    s["text"] = t
                    replaced_solved.append(pid)
                    sf.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        pj.write_text(json.dumps(probs, ensure_ascii=False, indent=2), encoding="utf-8")

    def key(pid: str):
        a, b = pid.split(".")
        return int(a), int(b)

    report = {
        "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "parsed": len(parsed), "cropped": len(cropped), "matched": len(matched),
        # 切出来了但文本里没有：保持原来的题干（还没有题干的由系统看图转写）
        "missing_in_text": sorted(set(cropped) - set(parsed), key=key),
        # 文本里有但没切出来：切题可能漏了这些题，值得人工看一眼
        "missing_in_crop": sorted(set(parsed) - set(cropped), key=key),
        "duplicated_in_text": sorted(set(dup), key=key),
        # 文本里这些题的公式写了一半（对话框输出被截断/损坏）：没有导入，由系统按原图转写
        "damaged_in_text": sorted(damaged, key=key),
        # 已有解答的题换了题干：解答仍基于旧题干，需要时点“重解本题”
        "replaced_solved": sorted(replaced_solved, key=key),
        "check": check,
    }
    (p_dir / REPORT_FILE).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
