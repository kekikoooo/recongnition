# -*- coding: utf-8 -*-
"""
StudyHelp 多通道通用严谨解题引擎 (Universal Multi-Stage Solver Engine)
支持多学科动态提示词、草稿->苛刻评审->标杆定稿->速查四阶段闭环、线程池并发调度与防重断点续传
"""

import os
import re
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

from core.config import CHAPTERS_ROOT, load_book_config, get_chapter_config, get_book_metadata, get_active_project_dir
from core.prompt_factory import get_system_prompts

from google import genai
from google.genai import types

def init_gemini_client(cfg: Optional[Dict[str, Any]] = None):
    if cfg is None:
        cfg = load_book_config()
    # 请求超时：流式连接经代理可能卡死且永不返回，必须设上限让 _call 重试（毫秒）
    http = types.HttpOptions(timeout=int(os.environ.get("STUDYHELP_LLM_TIMEOUT_MS", "300000")))
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        return genai.Client(api_key=api_key, http_options=http)
    ai_cfg = cfg.get("ai", {})
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT", ai_cfg.get("project_id", "citric-biplane-358313"))
    loc = os.environ.get("GOOGLE_CLOUD_LOCATION", ai_cfg.get("location", "global"))
    return genai.Client(vertexai=True, project=proj, location=loc, http_options=http)

PASS_SCORE = 95          # 评审达到该分数才算通过
MAX_REVISIONS = 2        # 未通过时，带着评审意见重新推导的最大次数
_SCORE_RE = re.compile(r'(?:综合)?评分[】\]]?\s*[：:]\s*\**\s*(\d{1,3})')


def _model_name(cfg: Dict[str, Any]) -> str:
    return cfg.get("ai", {}).get("model", "gemini-3.8-flash")


# 各阶段的思考预算（token）：转写/核对只需看清图，思考少；推导首轮中等，未通过评审再加码。
THINK_BUDGET = {
    "transcribe": 2048, "verify": 4096, "crop_check": 4096, "compact": 1024, "review": 6000,
    "draft": 8000, "draft_easy": 4000, "draft_retry": 12000, "final": 12000, "locate": 4096,
}
# 题图分辨率：转写/核对要看清下标，用高分辨率；推导/评审已有文字题干，图只作参照，用中分辨率
MEDIA_RES = {"transcribe": "HIGH", "verify": "HIGH", "crop_check": "HIGH", "crop_fix": "HIGH", "locate": "HIGH"}
DEFAULT_MEDIA_RES = os.environ.get("STUDYHELP_MEDIA_RES", "MEDIUM")


def _budget(stage: str) -> int:
    env = os.environ.get("STUDYHELP_THINKING_BUDGET")  # 设了就统一覆盖（兼容旧配置）
    return int(env) if env else THINK_BUDGET.get(stage, 8000)


def _call(client, model: str, system: str, contents: list, temperature: float, tries: int = 8,
          stage: str = "other") -> str:
    from core import usage
    last = None
    base_stage = "draft" if stage.startswith("draft") else stage  # draft_easy / draft_retry 记为草稿
    media = MEDIA_RES.get(base_stage, DEFAULT_MEDIA_RES)
    from core.usage import STAGE_NAMES
    from core import events
    _pid = getattr(usage._ctx, "pid", None) or "-"
    _sn = STAGE_NAMES.get(base_stage, base_stage)
    for attempt in range(tries):
        um = None
        _t0 = time.time()
        _m = f"题{_pid} [{_sn}] 已发出（第 {attempt + 1}/{tries} 次，思考预算 {_budget(stage)}）"
        print(f"⚡ 请求 {_m}", flush=True)
        events.log("请求", _m)
        try:
            # 流式接收 + 回传思考摘要：难题模型先“思考”一两分钟才出第一个字，
            # 期间连接上没有数据会被代理按空闲超时(~90s)断开；思考摘要持续回传可保活，拼接时排除。
            parts = []
            cfg = types.GenerateContentConfig(
                system_instruction=system, temperature=temperature,
                media_resolution=getattr(types.MediaResolution, f"MEDIA_RESOLUTION_{media}", None),
                thinking_config=types.ThinkingConfig(include_thoughts=True, thinking_budget=_budget(stage)))
            for chunk in client.models.generate_content_stream(model=model, contents=contents, config=cfg):
                if getattr(chunk, "usage_metadata", None) is not None:
                    um = chunk.usage_metadata
                for cand in (chunk.candidates or [])[:1]:
                    for part in ((cand.content.parts if cand.content else None) or []):
                        if part.text and not getattr(part, "thought", False):
                            parts.append(part.text)
            usage.record(base_stage, um, model)
            text = "".join(parts).strip()
            _g = lambda k: int(getattr(um, k, None) or 0)
            _m = f"题{_pid} [{_sn}] 用时 {time.time() - _t0:.0f}s · 输入 {_g('prompt_token_count')} / 输出 {_g('candidates_token_count')} / 思考 {_g('thoughts_token_count')}"
            print(f"✓ 返回 {_m}", flush=True)
            events.log("返回", _m)
            if text:
                return text
            last = "空响应"
        except Exception as err:  # 网络/配额等
            usage.record(base_stage, um, model)  # 中途断开的调用也已计费
            last = err
            msg = str(err)
            _m = f"题{_pid} [{_sn}] 用时 {time.time() - _t0:.0f}s：{msg[:80]}，稍后重试"
            print(f"✗ 失败 {_m}", flush=True)
            events.log("失败", _m)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "disconnected" in msg.lower():
                time.sleep(min(180, 30 * (attempt + 1)))  # 配额/限流：长退避
            else:
                time.sleep(min(45, 5 * (attempt + 1) ** 1.5))
    raise RuntimeError(f"模型调用失败: {last}")


def _image_part(pages_dir: Path, pid: str, page_num: Any):
    for path in (pages_dir / f"problem_{pid}_slice.png", pages_dir / f"page_{page_num}.png"):
        if path.exists():
            return types.Part.from_bytes(data=path.read_bytes(), mime_type="image/png")
    return None


_COMPACT_HEAD_RE = re.compile(r'\n#{1,4}[^\n]*(?:速查|Compact)[^\n]*\n', re.IGNORECASE)


def derive_compact(full: str) -> str:
    """速查 = 定稿里“详尽分步演算推导”起的整段（含验算与最终结论），和 test1 的速查一致，不额外调用模型"""
    full = full or ""
    m = re.search(r"(?m)^#{2,4}\s*[^\n]*(?:详尽分步|分步演算|分步推导)[^\n]*$", full)
    return (full[m.start():] if m else full).strip()


def split_final_compact(full: str):
    """定稿与速查在同一次输出里；模型会自拟速查标题（如“极简紧凑速查版 (Compact Solution)”），
    所以匹配任何含“速查/Compact”的标题行，取最后一个。"""
    full = full or ""
    ms = list(_COMPACT_HEAD_RE.finditer(full))
    if not ms:
        return full.strip(), derive_compact(full)
    m = ms[-1]
    return full[:m.start()].strip(), full[m.end():].strip()


def parse_score(review_text: str) -> Optional[int]:
    """从评审文本中解析真实分数；解析不到返回 None（绝不伪造默认分）。"""
    m = _SCORE_RE.findall(review_text or "")
    if not m:
        return None
    val = int(m[-1])
    return val if 0 <= val <= 100 else None


def strip_leading_pid(text: str, pid: str) -> str:
    """去掉题干开头的题号（3.9 / 3-9 / 3－9 / 习题3.9 / 第3.9题，可能重复出现），网页与合订本会统一加题号。"""
    if not text or "." not in str(pid):
        return text or ""
    ch, no = str(pid).split(".", 1)
    pat = re.compile(rf"^\s*(?:\*\*)?(?:习题|第)?\s*{re.escape(ch)}\s*[-－—–﹣.．·]\s*{re.escape(no)}(?!\d)\s*(?:题)?(?:\*\*)?\s*[.。:：、]?\s*")
    for _ in range(2):
        text = pat.sub("", text, count=1)
    return normalize_blanks(text.strip())


BLANK = r"\_\_\_\_\_\_"  # 填空横线统一写法（Markdown 中安全，渲染为一段短横线）


def normalize_blanks(text: str) -> str:
    """填空题横线在文字层/模型输出里会变成几百个 \\_ 或 \\ ，导致模型复读失控、网页排版爆炸。统一压成一段短横线。"""
    if not text:
        return text or ""
    text = re.sub(r"\\underline\{(?:\s|\\[ ,;:!]|\\quad|\\qquad|~)*\}", r"\\underline{\\qquad\\qquad}", text)
    text = re.sub(r"(?:\\[ ,;:!]\s*){6,}", r"\\qquad\\qquad ", text)
    text = re.sub(r"(?:\\_){4,}", lambda m: BLANK, text)
    text = re.sub(r"_{6,}", lambda m: BLANK, text)
    text = re.sub(r"[ \t]{8,}", "  ", text)
    text = re.sub(r"(?:&emsp;\s*){3,}", "&emsp;&emsp; ", text)  # 模型复读出成百上千个 &emsp;
    return text


def digitize_problem_text(client, model: str, img_part, title: str, pid: str) -> str:
    """扫描版切片没有文字层：让多模态模型把题目逐字转写为 Markdown+LaTeX，供后续引用与检索。"""
    if img_part is None:
        return ""
    sys_p = "你是严谨的教材数字化助教，只做逐字转写，不解题，不添加任何说明。公式用 $...$ 或 $$...$$。"
    prompt = f"请把图片中《{title}》题目 {pid} 的题干完整转写为 Markdown（含全部小问、条件、数值，选择题的全部选项 A/B/C/D），开头不要写题号；填空横线写成 \\_\\_\\_\\_\\_\\_，不要输出成串的空格或下划线。图中的插图用【见原图】标注。逐个看清每个符号：下标与上标的每个字母（如 q 与 g、l 与 1、o 与 0）、希腊字母、撇号、正负号、≈ 与 =、数字和单位，不要凭常识“纠正”原文；但公式的一小部分（如求和号上限 ∞）若被切到图片边缘外，按完整数学式写出。切片上下边缘可能夹带相邻题目的零星符号（如上一题的下标、下一题求和号的上限），只写属于本题的内容。严格保持原图的排版：换行位置与原图一致；原图居中单独成行的公式用 $$...$$ 单独成行；原图同一行并排的内容（如并排的小问、并排的两个式子）仍写在同一行，用 &emsp;&emsp; 分隔；原图分行的小问每问单独一行。"
    try:
        return strip_leading_pid(_call(client, model, sys_p, [prompt, img_part], 0.0, tries=4, stage="transcribe"), pid)
    except Exception as e:
        print(f"[!] 题 {pid} 题干转写失败: {e}")
        return ""


def solve_single_problem(client, cfg: Dict[str, Any], problem_dict: Dict[str, Any], pages_dir: Path, force: bool = False) -> Dict[str, Any]:
    from core import usage
    pid = problem_dict["problem_id"]
    usage.set_context(Path(pages_dir).parent.parent, pid)  # 用量记到 output/<工作项>/usage_log.jsonl
    usage.begin_problem()
    ch_idx = problem_dict.get("section_idx", 1)
    ch_name = problem_dict.get("section_name", "")
    page_num = problem_dict.get("page", 1)
    text = problem_dict.get("text", "") or ""

    book_meta = get_book_metadata(cfg)
    title = book_meta["title"]
    prompts = get_system_prompts(book_meta, chapter_name=ch_name, doc_type=book_meta.get("doc_type", "book"))
    model = _model_name(cfg)
    temperature = cfg.get("ai", {}).get("temperature", 0.2)
    img = _image_part(pages_dir, pid, page_num)
    # 导入的题干且大模型标明这道题没有图：只发文字，不发切片图（省约 1.7 千 token/题）；有图或没标明就照常带图
    imported_text_only = problem_dict.get("text_source") in ("imported", "imported_check") and problem_dict.get("has_fig") is False
    solve_img = None if imported_text_only else img

    # 0. 题干：除非来自 PDF 文字层或已通过检验，一律由多模态模型对照原图完整转写
    if problem_dict.get("text_source") not in ("text_layer", "verified", "imported", "imported_check") or len(text.strip()) < 8:
        text = digitize_problem_text(client, model, img, title, pid) or text

    head = f"《{title}》{ch_name} 题目 {pid}"
    base = [f"【目标题目】{head}\n{text}" + ("\n（题目原图见附图，条件以原图为准。）" if solve_img is not None else "")]
    if solve_img is not None:
        base.append(solve_img)

    # 选择/填空/判断这类小题首轮少思考；没通过评审的轮次再加大思考预算
    easy = bool(re.search(r"选择|填空|判断", f"{ch_name}{problem_dict.get('section_name', '')}"))
    best = None
    feedback = ""
    for round_no in range(MAX_REVISIONS + 1):
        # 1. 草稿（按定稿结构书写；后续轮次带上一轮的评审意见）
        d_contents = list(base)
        if feedback:
            d_contents.insert(1, f"【上一轮评审指出的问题，必须逐条修正】\n{feedback}")
        d_stage = "draft_retry" if round_no else ("draft_easy" if easy else "draft")
        draft = _call(client, model, prompts["draft"], d_contents, temperature, stage=d_stage)
        draft_body, draft_compact = split_final_compact(draft)

        # 2. 独立评审：单独的一次调用，只给题目和草稿（不含速查）
        r_contents = list(base) + [f"【待评审草稿】\n{draft_body}",
                                   "请逐项审查后，最后一行严格写成：【评分】：NN分（NN 为 0~100 的整数）。"]
        review = _call(client, model, prompts["review"], r_contents, 0.1, stage="review")
        score = parse_score(review)
        if score is None:  # 评审没给分数 -> 再要一次，仍没有就记为未评审
            review2 = _call(client, model, prompts["review"], r_contents, 0.0, stage="review")
            score = parse_score(review2)
            if score is not None:
                review = review2

        cand = {"draft": draft_body, "compact": draft_compact, "review": review, "score": score, "rounds": round_no + 1}
        if best is None or (score or -1) > (best["score"] or -1):
            best = cand
        if score is not None and score >= PASS_SCORE:
            break
        feedback = review[-1800:]

    score = best["score"]
    passed = score is not None and score >= PASS_SCORE
    if passed:
        # 3a. 评审通过：草稿本身就是定稿结构，直接发布，不再整篇重写
        final_text, compact_text = best["draft"], best["compact"]
        if not compact_text.strip():
            compact_text = derive_compact(final_text)
        final_mode = "draft_passed"
    else:
        # 3b. 未通过：带着最佳草稿与评审意见重写定稿 + 速查
        f_contents = list(base) + [f"【最佳草稿】\n{best['draft']}", f"【评审意见】\n{best['review']}",
                                   "请输出定稿。"]
        final_full = _call(client, model, prompts["final"] + "\n\n" + prompts["compact"], f_contents, temperature,
                           stage="final")
        final_text, compact_text = split_final_compact(final_full)
        final_mode = "rewritten"

    status = "passed" if passed else ("needs_review" if score is not None else "unreviewed")
    return {
        "problem_id": pid, "section_idx": ch_idx, "section_name": ch_name, "page": page_num,
        "text": text, "draft": best["draft"], "review": best["review"], "final": final_text,
        "compact": compact_text, "score": score if score is not None else "", "status": status,
        "review_rounds": best["rounds"], "final_mode": final_mode, "usage": usage.problem_usage(),
        "timestamp": time.time(),
    }

def solve_chapter(ch_key: Any, concurrency: int = 4, force: bool = False, cfg: Optional[Dict[str, Any]] = None, project_dir: Optional[Path] = None, stop_flag: Optional[Any] = None, on_start: Optional[Any] = None, on_progress: Optional[Any] = None):
    p_dir = Path(project_dir) if project_dir else get_active_project_dir()
    if cfg is None:
        cfg = load_book_config(p_dir)
    ch_idx = int(ch_key) if str(ch_key).isdigit() else 1
    ch_dir = p_dir / f"Chapter_{ch_idx:02d}"
    pages_dir = ch_dir / "pages"
    slots_dir = ch_dir / "slots"
    slots_dir.mkdir(parents=True, exist_ok=True)
    problems_json = ch_dir / "problems.json"

    if not problems_json.exists():
        print(f"[-] 章节 {ch_key} 尚未执行切片，未找到 problems.json，跳过。")
        return {"total": 0, "completed": 0, "ok": True}

    with open(problems_json, "r", encoding="utf-8") as f:
        problems = json.load(f)

    client = init_gemini_client(cfg)
    ch_name = cfg.get("chapters", {}).get(str(ch_idx), {}).get("name", "")
    todo = []
    for p in problems:
        p.setdefault("section_idx", ch_idx)
        p.setdefault("section_name", ch_name)
        pid = p["problem_id"]
        slot_file = slots_dir / f"slot_{pid}.json"
        if slot_file.exists() and not force:
            continue
        todo.append(p)

    print(f"[*] 章节 {ch_key} 总计 {len(problems)} 题，待求解 {len(todo)} 题 (并发通道: {concurrency})...")

    def _worker(prob):
        if stop_flag and stop_flag():
            return prob["problem_id"], False, "paused"
        pid = prob["problem_id"]
        slot_file = slots_dir / f"slot_{pid}.json"
        if on_start:
            try:
                on_start(pid)
            except Exception:
                pass
        try:
            res = solve_single_problem(client, cfg, prob, pages_dir, force=force)
            with open(slot_file, "w", encoding="utf-8") as sf:
                json.dump(res, sf, ensure_ascii=False, indent=2)
            print(f"[✓] 题目 {pid} 求解定稿完成！")
            from core import events as _ev
            _ev.log("题目", f"题{pid} 完成（评审 {res.get('score', '-') if isinstance(res, dict) else '-'} 分，{res.get('final_mode', '') if isinstance(res, dict) else ''}）")
            if on_progress:
                try:
                    on_progress(pid, True)
                except Exception:
                    pass
            return pid, True, None
        except Exception as e:
            print(f"[✗] 题目 {pid} 求解出错: {e}")
            from core import events as _ev
            _ev.log("失败", f"题{pid} 求解出错：{str(e)[:80]}")
            if on_progress:
                try:
                    on_progress(pid, False)
                except Exception:
                    pass
            return pid, False, str(e)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_worker, p) for p in todo]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"[!] 线程执行异常: {e}")

    print(f"[✓] 章节 {ch_key} 求解流水线执行完毕。")
    return {"total": len(problems), "completed": len(list(slots_dir.glob("slot_*.json"))), "ok": True}

def solve_all_book(concurrency: int = 4, force: bool = False, cfg: Optional[Dict[str, Any]] = None, project_dir: Optional[Path] = None, stop_flag: Optional[Any] = None, on_start: Optional[Any] = None, on_progress: Optional[Any] = None, on_chapter_change: Optional[Any] = None):
    """
    全卷/全书全自动端到端贯通求解引擎：
    按章节序号自动串联推进，直至全卷所有题目全部定稿入库并编译合订本。
    """
    p_dir = Path(project_dir) if project_dir else get_active_project_dir()
    if cfg is None:
        cfg = load_book_config(p_dir)
    
    ch_dirs = sorted([d for d in p_dir.glob("Chapter_*") if d.is_dir() and (d / "problems.json").exists()])
    total_all = 0
    completed_all = 0

    print(f"[*] 启动全卷/全书全自动求解流水线，共检测到 {len(ch_dirs)} 个章节/大题...")

    for ch_dir in ch_dirs:
        m = re.search(r'Chapter_(\d+)', ch_dir.name)
        if not m:
            continue
        ch_idx = int(m.group(1))
        if stop_flag and stop_flag():
            print(f"[-] 收到中断信号，全卷求解在 Chapter {ch_idx} 处暂停。")
            break

        if on_chapter_change:
            try:
                on_chapter_change(ch_idx)
            except Exception:
                pass

        res = solve_chapter(
            ch_idx,
            concurrency=concurrency,
            force=force,
            cfg=cfg,
            project_dir=p_dir,
            stop_flag=stop_flag,
            on_start=on_start,
            on_progress=on_progress
        )
        total_all += res.get("total", 0)
        completed_all += res.get("completed", 0)

        # 实时编译该章节与全书 Markdown 合订本
        try:
            from core.aggregator import aggregate_chapter, aggregate_all_book
            aggregate_chapter(ch_idx, cfg, project_dir=p_dir)
            aggregate_all_book(p_dir)
        except Exception as e:
            print(f"[-] 聚合合订本警告: {e}")

    print(f"[✓] 全卷/全书自动求解结束：共完成 {completed_all}/{total_all} 题！")
    return {"total": total_all, "completed": completed_all, "ok": True}


def backfill_problem_texts(project_dir: Path, cfg: Optional[Dict[str, Any]] = None) -> int:
    """为题干为空的题补做多模态转写（求解时转写可能因限流失败），写回 slot 和 problems.json。"""
    p_dir = Path(project_dir)
    cfg = cfg or load_book_config(p_dir)
    title = get_book_metadata(cfg)["title"]
    model = _model_name(cfg)
    client = None
    filled = 0
    for pj in sorted(p_dir.glob("Chapter_*/problems.json")):
        ch_dir = pj.parent
        probs = json.loads(pj.read_text(encoding="utf-8"))
        changed = False
        for prob in probs:
            pid = prob["problem_id"]
            sf = ch_dir / "slots" / f"slot_{pid}.json"
            slot = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else None
            text = (slot or {}).get("text") or prob.get("text") or ""
            if len(text.strip()) < 8:
                from core import usage
                usage.set_context(p_dir, pid)
                client = client or init_gemini_client(cfg)
                text = digitize_problem_text(client, model, _image_part(ch_dir / "pages", pid, prob.get("page")), title, pid)
                if text:
                    filled += 1
            if text and prob.get("text") != text:
                prob["text"] = text
                changed = True
            dirty = False
            if slot is not None and text and slot.get("text") != text:
                slot["text"] = text
                dirty = True
            if slot is not None and not (slot.get("compact") or "").strip():
                fin, comp = split_final_compact(slot.get("final", ""))
                if comp:  # 旧版拆分规则漏拆的速查，从定稿尾部拆出来
                    slot["final"], slot["compact"] = fin, comp
                    dirty = True
            if dirty:
                sf.write_text(json.dumps(slot, ensure_ascii=False, indent=2), encoding="utf-8")
        if changed:
            pj.write_text(json.dumps(probs, ensure_ascii=False, indent=2), encoding="utf-8")
    return filled
