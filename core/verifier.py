# -*- coding: utf-8 -*-
"""
最终检验环节：流水线最后一步，逐题核对产物是否可交付，能修的自动修，修不了的写进报告。

逐题检查：
1. 切片图存在且尺寸正常；
2. 题干文字：让多模态模型对照原题切片核对“是否完整、与原图一致”（含全部小问、条件、数值、公式），
   不完整就用模型给出的完整转写替换（写回 problems.json 与 slot），并标记 text_source=verified；
3. 解答：slot 存在，定稿/速查非空，评审状态。
输出 verify_report.json，并返回汇总，供进度条与最终状态显示。
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image

from core.config import load_book_config, get_book_metadata
from core.solver import (init_gemini_client, _call, _model_name, _image_part, strip_leading_pid, normalize_blanks)

CHECK_SYSTEM = ("你是严谨的教材校对员。只做核对与逐字转写，不解题。"
                "公式用 $...$ 或 $$...$$ 的标准 LaTeX。严格按用户要求的 COMPLETE/ISSUES/TEXT 三段纯文本格式回答，"
                "不要输出 JSON，不要用代码块。")


def _check_prompt(title: str, pid: str, text: str) -> str:
    return f"""附图是《{title}》题目 {pid} 的原书切片。下面是系统里保存的题干文字：
<<<
{text or "（空）"}
>>>
请逐项对照原图核对这段题干：是否包含原图中的全部内容（题干主体、所有条件与数值、公式、全部小问、选择题的全部选项），有无遗漏、错字、公式错误或多余内容（图中题号不算内容）。
公式和数值必须逐个符号比对，不能只看大意：每个下标/上标的每个字母（q 与 g、l 与 1 与 I、o 与 0、v 与 ν、u 与 μ 这类形近字最容易错）、希腊字母、撇号与帽子、正负号、≈ 与 =、≤ 与 <、数字的每一位、单位。只要有一个符号与原图不同，COMPLETE 就写 no，并在 ISSUES 里写出“原文是什么、现在写成了什么”。
以下不算差异，不要因此判 no：字体样式（正体/斜体/粗体、\\mathrm、\\text、\\limits）、空格、全半角标点、括号大小写法；普通字母 i、j 照常写，不要改成 \\imath、\\jmath。
如果公式的一小部分（如求和号的上限 ∞、积分上下限）恰好被切到图片边缘之外或只露出一点，按完整的数学式保留，不算多写。切片上下边缘可能夹带相邻题目的零星符号（如上一题的下标、下一题求和号的上限），只写属于本题的内容。只核对内容，不必纠正换行或并排方式（排版由程序按原图坐标处理）。填空题的横线一律写成 \\_\\_\\_\\_\\_\\_，不要输出成串的空格或下划线。
严格按下面三段格式输出（不要用 JSON，不要用代码块）：
COMPLETE: yes 或 no
ISSUES: 发现的问题，没有就写“无”
TEXT:
若 COMPLETE 为 no，在这里给出对照原图的完整准确题干（Markdown+LaTeX，开头不写题号，插图处写【见原图】；严格保持原图的排版：换行位置与原图一致；原图居中单独成行的公式用 $$...$$ 单独成行；原图同一行并排的内容（如并排的小问、并排的两个式子）仍写在同一行，用 &emsp;&emsp; 分隔；原图分行的小问每问单独一行。）；为 yes 则留空"""


def _parse_verdict(s: str) -> Optional[Dict[str, Any]]:
    """解析 COMPLETE / ISSUES / TEXT 三段格式。
    不用 JSON：LaTeX 的 \text、\frac、\beta 在 JSON 里会被当成 \t \f \b 转义吃掉。"""
    s = (s or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s)
    # 兼容模型偶尔仍用 JSON 风格回答（"COMPLETE": "yes"）
    m = re.search(r"COMPLETE\"?\s*[:：]\s*\"?(yes|no|是|否|true|false)", s, re.I)
    if not m:
        return None
    complete = m.group(1).lower() in ("yes", "是", "true")
    mi = re.search(r"ISSUES\s*[:：]\s*(.*?)(?=\n\s*TEXT\s*[:：]|\Z)", s, re.S | re.I)
    issues = (mi.group(1).strip() if mi else "")
    if issues in ("无", "没有", "none", "None"):
        issues = ""
    mt = re.search(r"\n\s*TEXT\s*[:：]\s*(.*)\Z", s, re.S | re.I)
    text = (mt.group(1).strip() if mt else "")
    return {"complete": complete, "issues": issues, "text": text}


def _check_problem(client, model: str, title: str, ch_dir: Path, prob: Dict[str, Any],
                   check_solution: bool = True) -> Dict[str, Any]:
    pid = str(prob["problem_id"])
    res: Dict[str, Any] = {"problem_id": pid, "issues": [], "fixed": []}
    slice_path = ch_dir / "pages" / f"problem_{pid}_slice.png"
    if not slice_path.exists():
        res["issues"].append("缺少原题切片图")
    else:
        w, h = Image.open(slice_path).size
        if h < 25 or w < 200:
            res["issues"].append(f"切片尺寸异常 {w}x{h}")

    slot_file = ch_dir / "slots" / f"slot_{pid}.json"
    slot = json.loads(slot_file.read_text(encoding="utf-8")) if slot_file.exists() else None
    text = normalize_blanks((prob.get("text") or (slot or {}).get("text") or "").strip())

    already_ok = prob.get("text_source") in ("verified", "imported") and len(text) >= 8 and not re.search(r"[\t\x08\x0b\x0c\r]", text)
    if already_ok:
        res["text_ok"] = True

    # 题干核对（有切片才能核对；已通过检验的跳过）
    if slice_path.exists() and not already_ok:
        img = _image_part(ch_dir / "pages", pid, prob.get("page"))
        if len(text) < 8:
            # 还没有题干（扫描件刚切完）：先独立转写一遍，再由下面的核对环节逐符号复核
            from core.solver import digitize_problem_text
            text = normalize_blanks(digitize_problem_text(client, model, img, title, pid))
            if text:
                res["fixed"].append("题干已按原图转写")
        old_text = text
        verdict = None
        for _ in range(2):
            try:
                verdict = _parse_verdict(_call(client, model, CHECK_SYSTEM, [_check_prompt(title, pid, text), img], 0.0,
                                               tries=4, stage="verify"))
            except Exception as e:
                res["issues"].append(f"题干核对调用失败: {e}")
                break
            if verdict is not None:
                break
        if verdict is not None:
            new_text = strip_leading_pid((verdict.get("text") or "").strip(), pid)
            if verdict.get("complete") is False and len(new_text) >= 8:
                res["fixed"].append("题干不完整/有误，已按原图重新转写" + (f"（{verdict.get('issues')}）" if verdict.get("issues") else ""))
                text = new_text
            elif verdict.get("complete") is False:
                res["issues"].append(f"题干不完整但未能自动修正: {verdict.get('issues')}")
            res["text_ok"] = verdict.get("complete") is not False or len(new_text) >= 8
        elif not any("调用失败" in x for x in res["issues"]):
            res["issues"].append("题干核对结果无法解析")
        solved_text = normalize_blanks(((slot or {}).get("text") or "").strip())
        if slot is not None and text != old_text and solved_text and solved_text != text:
            res["issues"].append("题干已按原图更正，现有解答基于旧题干，建议点“重解本题”")
            res["stale_solution"] = True
    # 排版：并排关系由代码按原图小问坐标决定（移植自 test1），不交给模型判断
    # 导入的题干（提示词已要求按原图排版并自查）不再做本地 OCR 对齐：每题要花几秒到几十秒，且是重复检查
    if text and slice_path.exists() and prob.get("text_source") not in ("imported", "imported_check"):
        from core.layout_align import align_problem_text
        aligned = align_problem_text(text, slice_path)
        if aligned != text:
            text = aligned
            res["fixed"].append("按原图小问位置对齐排版")
            res["text_ok"] = True if res.get("text_ok") is not False else res["text_ok"]
    res["text"] = text

    # 解答检查
    if not check_solution:
        return res
    if slot is None:
        res["issues"].append("没有解答（求解失败）")
    else:
        # 兼容旧版 slot 字段（stage3 / compact_breakdown），与网页、汇编的读取规则一致
        final_txt = (slot.get("final") or slot.get("stage3") or slot.get("stage3_deduction") or "").strip()
        compact_txt = (slot.get("compact") or slot.get("compact_breakdown") or "").strip()
        if not final_txt:
            res["issues"].append("定稿为空")
        if not compact_txt and final_txt:
            # 模型偶尔漏写速查：按定稿补生成一份，写回 slot
            try:
                from core.prompt_factory import get_system_prompts
                prompts = get_system_prompts({"title": title}, doc_type="book")
                comp = _call(client, model, prompts["compact"], [f"【标准解答】\n{final_txt}"], 0.2, tries=4, stage="compact")
                if comp.strip():
                    slot["compact"] = comp.strip()
                    slot_file.write_text(json.dumps(slot, ensure_ascii=False, indent=2), encoding="utf-8")
                    res["fixed"].append("速查为空，已按定稿补生成")
            except Exception as e:
                res["issues"].append(f"速查为空，补生成失败: {e}")
        elif not compact_txt:
            res["issues"].append("速查为空")
        if slot.get("status") in ("needs_review", "unreviewed"):
            res["issues"].append(f"评审未达标（{slot.get('score') or '无分数'}）")
    return res


def verify_project(project_dir: Path, cfg: Optional[Dict[str, Any]] = None, concurrency: int = 4,
                   on_progress: Optional[Callable[[int, int], None]] = None,
                   check_solutions: bool = True, only: Optional[set] = None) -> Dict[str, Any]:
    """check_solutions=False：只做题干（切完题后、解题前跑，让解题用上核对过的题干）。
    only：只检验这些题号（重解单题后用）。"""
    p_dir = Path(project_dir)
    cfg = cfg or load_book_config(p_dir)
    title = get_book_metadata(cfg)["title"]
    model = _model_name(cfg)
    client = init_gemini_client(cfg)

    jobs = []
    for pj in sorted(p_dir.glob("Chapter_*/problems.json")):
        for prob in json.loads(pj.read_text(encoding="utf-8")):
            if only is None or str(prob["problem_id"]) in only:
                jobs.append((pj.parent, prob))
    total, done = len(jobs), 0
    results: List[Dict[str, Any]] = []

    def work(job):
        from core import usage
        usage.set_context(p_dir, job[1]["problem_id"])
        return job[0], _check_problem(client, model, title, job[0], job[1], check_solution=check_solutions)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for ch_dir, r in ex.map(work, jobs):
            results.append(dict(r, chapter=ch_dir.name))
            done += 1
            if on_progress:
                on_progress(done, total)

    # 写回修正后的题干
    by_dir: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in results:
        by_dir.setdefault(r["chapter"], {})[r["problem_id"]] = r
    for ch_name, rs in by_dir.items():
        ch_dir = p_dir / ch_name
        pj = ch_dir / "problems.json"
        probs = json.loads(pj.read_text(encoding="utf-8"))
        for prob in probs:
            r = rs.get(str(prob["problem_id"]))
            if not r or not r.get("text"):
                continue
            if r.get("text_ok"):
                # 导入且免核对的题干保留来源标记（网页据此显示“导入”）
                src = "imported" if prob.get("text_source") == "imported" else "verified"
                prob["text"], prob["text_source"] = r["text"], src
            sf = ch_dir / "slots" / f"slot_{prob['problem_id']}.json"
            if sf.exists() and r.get("text_ok"):
                slot = json.loads(sf.read_text(encoding="utf-8"))
                if slot.get("text") != r["text"]:
                    slot["text"] = r["text"]
                    if r.get("stale_solution"):
                        slot["stale_text"] = True  # 解答基于旧题干，网页提示重解
                    sf.write_text(json.dumps(slot, ensure_ascii=False, indent=2), encoding="utf-8")
        pj.write_text(json.dumps(probs, ensure_ascii=False, indent=2), encoding="utf-8")

    fixed = [r["problem_id"] for r in results if r["fixed"]]
    problems = {r["problem_id"]: r["issues"] for r in results if r["issues"]}
    details = [{k: v for k, v in r.items() if k != "text"} for r in results]
    rep_file = p_dir / "verify_report.json"
    if only is not None and rep_file.exists():
        # 只检验了部分题：并入原报告，其它题的结论保留
        try:
            old = json.loads(rep_file.read_text(encoding="utf-8"))
            keep = [d for d in old.get("details", []) if str(d.get("problem_id")) not in only]
            details = keep + details
            fixed_all = [d["problem_id"] for d in details if d.get("fixed")]
            problems_all = {d["problem_id"]: d["issues"] for d in details if d.get("issues")}
            report = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "total": len(details),
                      "auto_fixed": fixed_all, "needs_attention": problems_all, "details": details}
            rep_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"total": total, "fixed": len(fixed), "attention": len(problems)}
        except Exception:
            pass
    report = {
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": total, "auto_fixed": fixed, "needs_attention": problems, "details": details,
    }
    rep_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"total": total, "fixed": len(fixed), "attention": len(problems),
            "transcribed": sum(1 for r in results if any("已按原图转写" in f for f in r["fixed"])),
            "corrected": sum(1 for r in results if any("重新转写" in f for f in r["fixed"]))}
