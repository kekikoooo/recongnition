# -*- coding: utf-8 -*-
"""
上传后的端到端后台任务：切题 -> 求解(带评审闸门) -> 聚合 Markdown -> 编译 PDF。
状态通过 INGEST 字典暴露给 /api/ingest/status，前端据此显示进度。
"""

import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

INGEST: Dict[str, Any] = {
    "is_running": False, "project": None, "stage": "idle", "message": "", "pct": 0,
    "error": None, "started": 0.0, "report": None, "pdf": {}, "verify": None, "notes": [],
}
_LOCK = threading.Lock()


def _set(**kw):
    INGEST.update(kw)
    try:
        from core import events
        if "stage" in kw or "is_running" in kw:
            events.log("阶段", f"{INGEST.get('stage')} · {INGEST.get('message') or ''} · {INGEST.get('pct')}%")
        elif "message" in kw:
            events.log("提示", f"{INGEST.get('stage')} · {kw['message']}", throttle=10, key="msg")
        if kw.get("error"):
            events.log("失败", f"构建出错：{kw['error']}")
    except Exception:
        pass


def _counts(proj_dir: Path):
    import json
    n_prob = sum(len(json.loads(p.read_text(encoding="utf-8"))) for p in proj_dir.glob("Chapter_*/problems.json"))
    n_slot = len(list(proj_dir.glob("Chapter_*/slots/slot_*.json")))
    return n_prob, n_slot


def run_ingest(proj_dir: Path, solve: bool = True, concurrency: int = 20, skip_crop: bool = False,
               sniff: Optional[Dict[str, Any]] = None):
    """在线程里运行；任何阶段失败都写入 INGEST['error']，已完成的产物保留。
    顺序：切题 -> 题干转写+逐符号核对 -> （solve=False 时到此为止，等人看过切片再解题）
          -> 求解(评审闸门) -> 解答检验 -> 汇编 MD -> 编译 PDF。
    skip_crop：已切过题（继续构建/开始解题）时不重切，保留已核对的切片与题干。"""
    from core.config import load_book_config
    from core.cropper import UniversalCropper
    from core.solver import solve_all_book
    from core.aggregator import aggregate_all_book
    from core.pdf_compiler import compile_all_suite_pdfs
    import json
    import os

    proj_dir = Path(proj_dir)
    _set(is_running=True, project=proj_dir.name, stage="crop", message="正在识别并切分习题…",
         pct=2, error=None, started=time.time(), report=None, pdf={}, verify=None, crop_only=not solve)
    try:
        from core.verifier import verify_project
        if sniff:
            # 书籍嗅探（标题/学科/章节大纲）；失败就保留上传时写的最小 profile，切题阶段会回填章节
            _set(message="正在识别文档结构…")
            try:
                from core.auto_profiler import auto_generate_and_save_profile
                pdf0 = next(p for p in sorted(proj_dir.glob("*.pdf")) if not p.name.startswith(("Book_", "Chapter_")))
                auto_generate_and_save_profile(str(pdf0), proj_dir / "profile.json", **sniff)
            except Exception as e:
                print(f"[!] 自动嗅探 profile 失败，使用最小 profile: {e}")
        has_crop = (proj_dir / "crop_report.json").exists() and any(proj_dir.glob("Chapter_*/problems.json"))
        if not (skip_crop and has_crop):
            pdfs = sorted(proj_dir.glob("*.pdf"))
            pdfs = [p for p in pdfs if not p.name.startswith(("Book_", "Chapter_"))]
            if not pdfs:
                raise FileNotFoundError("项目目录下没有 PDF")
            import re as _re

            def _crop_log(*a):
                line = " ".join(str(x) for x in a)
                print(line, flush=True)
                m = _re.search(r"扫描进度\s*(\d+)/(\d+)", line)
                if m:
                    d, t = int(m.group(1)), int(m.group(2))
                    _set(message=f"OCR 扫描页面 {d}/{t}（扫描版整本书要几分钟到几十分钟，可以先做别的）", pct=3 + int(22 * d / max(1, t)))
                elif line.strip().startswith("[*]"):
                    _set(message=line.strip()[3:].strip()[:80])

            only = expected = None
            imp0 = proj_dir / "timu_import.md"
            if imp0.exists():
                import pymupdf as _pm
                from core.timu_import import parse_page_ranges, chapters_in_text
                with _pm.open(str(pdfs[0])) as _d:
                    _txt = imp0.read_text(encoding="utf-8")
                    only = parse_page_ranges(_txt, len(_d), only_chapters=chapters_in_text(_txt)) or None
                    from core.timu_import import parse_timu
                    expected = {tuple(int(x) for x in k.split(".")) for k in parse_timu(_txt)[0]} or None
            loc = None
            if only:
                _set(message=f"按导入文件的习题页范围 OCR 扫描 {len(only)} 页…")
            else:
                uc = UniversalCropper(str(pdfs[0]), log=_crop_log)
                import pymupdf as _pm
                with _pm.open(str(pdfs[0])) as _d:
                    n_pages = len(_d)
                if uc._doc_type() == "book" and n_pages > 40 and not uc._has_text_layer():
                    # 扫描版整本书：先让大模型看目录找习题页，只 OCR 这些页
                    _set(message="正在看目录定位习题页（一次模型调用，约 1 分钟）…")
                    from core.page_locator import locate_exercise_pages, apply_chapter_names
                    loc = locate_exercise_pages(pdfs[0], proj_dir, log=_crop_log)
                    if loc:
                        only = set(loc["pages"])
                        apply_chapter_names(proj_dir, loc)
                        _set(message=f"目录定位到习题 {len(loc['ranges'])} 处、共 {len(only)}/{n_pages} 页，只扫描这些页…")
                if not only:
                    _set(message="正在 OCR 扫描全书页面…")
            res = UniversalCropper(str(pdfs[0]), log=_crop_log, only_pages=only,
                                   expected_ids=expected if only else None).crop_all()
            got = {str(k) for k, v in (res.items() if isinstance(res, dict) else []) if v}
            want = {str(r["chapter"]) for r in (loc or {}).get("ranges", []) if r.get("chapter")}
            if only and (not got or (want and len(got & want) < 0.6 * len(want))):
                # 按范围没切到题（或大半章没切到）：多半是页码定位错了，退回全书扫描（已扫的页有缓存）
                _set(message=f"习题页范围里只切到 {len(got)} 章，退回全书扫描…")
                if loc:
                    (proj_dir / "exercise_pages.json").unlink(missing_ok=True)
                UniversalCropper(str(pdfs[0]), log=_crop_log).crop_all()
        # 导入的文本只有部分章（例如只做了前 4 章）：其余章不做，挪到“_未导入章节”里（不删，随时可挪回来）
        imp1 = proj_dir / "timu_import.md"
        if imp1.exists() and not skip_crop:
            from core.timu_import import chapters_in_text
            keep = chapters_in_text(imp1.read_text(encoding="utf-8"))
            if keep:
                import shutil as _sh
                park = proj_dir / "_未导入章节"
                for d in sorted(proj_dir.glob("Chapter_*")):
                    try:
                        if d.is_dir() and int(d.name.split("_")[1]) not in keep:
                            park.mkdir(exist_ok=True)
                            _sh.move(str(d), str(park / d.name))
                    except Exception as _e:
                        print(f"[-] 挪开未导入章节失败 {d.name}: {_e}")
        rep = proj_dir / "crop_report.json"
        if rep.exists():
            _set(report=json.loads(rep.read_text(encoding="utf-8")))

        # 导入的题干（大模型对话框导出的全部题目）：按题号写进各题，没对上的题仍走看图转写
        imp = proj_dir / "timu_import.md"
        if imp.exists() and not (skip_crop and has_crop):
            from core.timu_import import apply_import
            pending = json.loads((proj_dir / "timu_import_pending.json").read_text(encoding="utf-8"))                 if (proj_dir / "timu_import_pending.json").exists() else {}
            r = apply_import(proj_dir, imp.read_text(encoding="utf-8"), check=bool(pending.get("check")))
            _set(timu_import=r, message=f"已导入题干 {r['matched']}/{r['cropped']} 题")
                # 题干：先转写并逐符号核对，解题直接用核对过的题干（不再带着错题干去解）
        from core.final_check import compute_notes
        try:
            from core.exercise_pdf import build as _build_ex
            _build_ex(proj_dir)                  # 习题页 PDF：给对话框转写用，本地处理不花 token
        except Exception as _e:
            print(f"[-] 习题页 PDF 生成失败: {_e}")
        _set(notes=compute_notes(proj_dir))      # 切题缺号 / 导入对账：切完立刻给出，不等整本做完
        _set(stage="text", message="正在整理题干（导入的直接使用，没导入的看图转写，并按原图对齐排版）…", pct=12)
        vt = verify_project(proj_dir, load_book_config(proj_dir), concurrency=concurrency, check_solutions=False,
                            on_progress=lambda d, t: _set(message=f"整理题干 {d}/{t}（导入的不花 token）", pct=12 + int(16 * d / max(1, t))))
        if not solve:
            n_prob, _ = _counts(proj_dir)
            n_imp = sum(1 for pj in proj_dir.glob("Chapter_*/problems.json")
                        for p in json.loads(pj.read_text(encoding="utf-8")) if p.get("text_source") == "imported")
            parts = []
            if n_imp:
                parts.append(f"导入 {n_imp} 题")
            if vt.get("transcribed"):
                parts.append(f"看图转写 {vt['transcribed']} 题")
            if vt.get("corrected"):
                parts.append(f"核对时更正 {vt['corrected']} 题")
            _set(stage="done", pct=100, verify=vt,
                 message=f"切题完成：共 {n_prob} 题；题干" + ("、".join(parts) or "已就绪") +
                         "。请在左侧逐题检查切片，确认后点“开始解题”")
            return

        cfg = load_book_config(proj_dir)
        if solve:
            _set(stage="solve", message="正在逐题求解并评审…", pct=30)
            total = sum(len(json.loads(p.read_text(encoding="utf-8")))
                        for p in proj_dir.glob("Chapter_*/problems.json"))

            def _progress(pid, ok):
                done = len(list(proj_dir.glob("Chapter_*/slots/slot_*.json")))
                _set(message=f"求解中 {done}/{total}", pct=30 + int(55 * done / max(1, total)))

            solve_all_book(concurrency=concurrency, cfg=cfg, project_dir=proj_dir,
                           stop_flag=lambda: False, on_progress=_progress)
            from core.solver import backfill_problem_texts
            _set(message="补全题干文字…")
            backfill_problem_texts(proj_dir, load_book_config(proj_dir))
            missing = total - len(list(proj_dir.glob("Chapter_*/slots/slot_*.json")))
            if missing > 0:
                _set(message=f"有 {missing} 题求解失败，可稍后点“继续构建”补跑")

        # 最终检验：逐题对照原图核对题干完整性并自动修正，检查解答是否齐全
        _set(stage="verify", message="正在逐题检验（题干对照原图、解答完整性）…", pct=86)
        vs = verify_project(proj_dir, load_book_config(proj_dir), concurrency=concurrency,
                            on_progress=lambda d, t: _set(message=f"检验中 {d}/{t}"))
        _set(verify=vs)

        _set(stage="aggregate", message="正在汇编 Markdown 合订本…", pct=90)
        aggregate_all_book(proj_dir)

        _set(stage="pdf", message="正在编译 PDF…", pct=92)
        try:
            from core import pdf_compiler as _pc
            _pc.PROGRESS = lambda msg, pct: _set(message=msg, pct=pct)
            try:
                _set(pdf=compile_all_suite_pdfs(proj_dir))
            finally:
                _pc.PROGRESS = None
        except Exception as e:  # PDF 失败不影响 MD 与网页
            _set(pdf={"error": str(e)})
        n_prob, n_slot = _counts(proj_dir)
        failed_pdf = [k for k, v in (INGEST.get("pdf") or {}).items() if v is not True]
        msg = f"完成：{n_slot}/{n_prob} 题已解答"
        if n_slot < n_prob:
            msg += f"，{n_prob - n_slot} 题失败可“继续构建”补跑"
        v = INGEST.get("verify") or {}
        if v:
            msg += f"；检验：自动修正 {v.get('fixed', 0)} 题"
            if v.get("attention"):
                msg += f"，{v['attention']} 题需人工查看（见 verify_report.json）"
        if failed_pdf:
            msg += f"；PDF 未成功: {', '.join(failed_pdf)}"
        from core.usage import summarize
        us = summarize(proj_dir)
        if us.get("recorded"):
            msg += f"；本书累计用量 {us['total'] / 1e4:.1f} 万 token"
        _set(notes=compute_notes(proj_dir))
        if INGEST.get("notes"):
            msg += f"；⚠ {len(INGEST['notes'])} 条切题提示，见切题检查页顶部"
        _set(stage="done", message=msg, pct=100)
    except Exception as e:
        traceback.print_exc()
        _set(stage="error", error=str(e), message=f"失败：{e}")
    finally:
        _set(is_running=False)


def start_ingest(proj_dir: Path, solve: bool = True, concurrency: int = 20, skip_crop: bool = False,
                 sniff: Optional[Dict[str, Any]] = None) -> bool:
    with _LOCK:
        if INGEST["is_running"]:
            return False
        INGEST["is_running"] = True
        INGEST["project"] = Path(proj_dir).name
    threading.Thread(target=run_ingest, args=(proj_dir, solve, concurrency, skip_crop, sniff), daemon=True).start()
    return True
