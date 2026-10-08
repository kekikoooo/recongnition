# -*- coding: utf-8 -*-
"""
StudyHelp 通用题库工作台后端服务器 (Universal Web Server)
1. 统一用户数据工程：各教材/试卷数据全量收归 output/[工作项名]/ 独立维护；
2. 纯净架构，模板外置，彻底告别单文件内嵌数千行前端代码；
3. 完全动态读取图书/试卷 Profile，自适应任意数量的章节或大题；
4. 多学科动态伴读名师，自适应当前题目与知识点；
5. 支持 VS Code 与系统默认程序一键打开合订本与打印版。
"""

import os
import sys
import json
import time
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
import re

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.config import (
    PROJECT_ROOT, DATA_ROOT, get_active_project_dir, list_all_projects,
    load_book_config, get_book_metadata, get_all_chapters, get_chapter_config
)
from core.solver import solve_chapter, init_gemini_client
from core.prompt_factory import get_system_prompts

app = FastAPI(title="StudyHelp 通用交互题库工作台")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8089", "http://localhost:8089"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# 挂载活跃项目的目录与静态资源（采用动态路由替代静态绑定，项目即时切换不丢切片）
@app.get("/chapters/{path:path}")
def serve_dynamic_chapter_file(path: str):
    p_dir = get_active_project_dir()
    file_path = p_dir / path
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    return JSONResponse(status_code=404, content={"error": "File not found", "path": path})

app.mount("/data", StaticFiles(directory=str(DATA_ROOT)), name="data")
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")

TEMPLATES_DIR = PROJECT_ROOT / "templates"

def _detect_vscode_path() -> Path:
    candidates = [
        Path(r"K:\AI\VSCode\Code.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft VS Code" / "Code.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    return Path(r"K:\AI\VSCode\Code.exe")

VSCODE_PATH = _detect_vscode_path()

# ----------------- 核心 API 路由 -----------------

@app.get("/", response_class=HTMLResponse)
def index_page():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/review")


def _legacy_index_page():
    index_html = TEMPLATES_DIR / "index.html"
    if not index_html.exists():
        return HTMLResponse("<h3>templates/index.html 未找到</h3>", status_code=404)
    return HTMLResponse(index_html.read_text(encoding="utf-8"))

@app.get("/api/projects")
def api_list_projects():
    """获取 output/ 目录下所有工作项列表（附带正在构建的标记）"""
    from core.ingest_job import INGEST
    items = list_all_projects()
    for it in items:
        it["building"] = bool(INGEST.get("is_running") and INGEST.get("project") == it["id"])
    return items


@app.post("/api/projects/open-folder")
def api_open_project_folder(req: Dict[str, str]):
    """在资源管理器中打开某个工作项的 output 文件夹（本地工具）"""
    proj = (req or {}).get("project_id") or get_active_project_dir().name
    target = (DATA_ROOT / proj).resolve()
    if DATA_ROOT.resolve() not in target.parents or not target.is_dir():
        return JSONResponse(status_code=404, content={"ok": False, "error": f"工作项不存在: {proj}"})
    try:
        os.startfile(str(target))
        return {"ok": True, "path": str(target)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.post("/api/projects/switch")
def api_switch_project(req: Dict[str, str]):
    """切换当前活跃项目"""
    proj_id = req.get("project_id")
    if not proj_id:
        return {"error": "Missing project_id"}
    target = DATA_ROOT / proj_id
    if not target.exists():
        return {"error": "Project not found"}
    os.environ["STUDYHELP_PROJECT"] = proj_id
    return {"ok": True, "active_project": proj_id}

@app.post("/api/upload")
async def api_upload_book(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    doc_type: Optional[str] = Form("auto"),
    subject: Optional[str] = Form("general"),
    auto_solve: Optional[str] = Form("1"),
    timu_file: Optional[UploadFile] = File(None),
    timu_check: Optional[str] = Form("0"),
):
    """上传 PDF -> 建项目目录 + profile -> 后台自动：切题 -> 求解(评审闸门) -> 汇编 MD -> 编译 PDF。
    接口立即返回，前端轮询 /api/ingest/status 查看进度。"""
    from core.ingest_job import start_ingest, INGEST
    try:
        if INGEST.get("is_running"):
            return JSONResponse(status_code=409, content={"ok": False, "error": "已有一本书正在构建，请等待完成"})
        filename = Path((file.filename or "uploaded_book.pdf").replace("\\", "/")).name
        if not filename.lower().endswith(".pdf"):
            return JSONResponse(status_code=400, content={"ok": False, "error": "仅支持 PDF 文件"})
        proj_name = (title or "").strip() or Path(filename).stem
        proj_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", proj_name).strip(" .") or "uploaded_book"

        target_proj_dir = DATA_ROOT / proj_name
        target_proj_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = target_proj_dir / filename
        pdf_path.write_bytes(await file.read())
        if doc_type not in ("book", "exam"):
            import pymupdf
            with pymupdf.open(str(pdf_path)) as _d:
                doc_type = "book" if len(_d) > 12 else "exam"

        # 先写最小 profile 立即返回；书籍嗅探（一次模型调用，可能较慢）放到后台任务里做，不阻塞网页
        profile = {"book": {"id": proj_name, "title": proj_name, "doc_type": doc_type or "book",
                            "subject": subject or "general", "dpi": 150}, "chapters": {}}
        (target_proj_dir / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        sniff = {"doc_type": doc_type, "subject": subject, "title": proj_name}
        # 可选：大模型对话框导出的全部题目文本，切完题后按题号导入
        if timu_file is not None and timu_file.filename:
            raw = await timu_file.read()
            (target_proj_dir / "timu_import.md").write_text(raw.decode("utf-8", errors="replace"), encoding="utf-8")
            (target_proj_dir / "timu_import_pending.json").write_text(
                json.dumps({"check": timu_check == "1"}), encoding="utf-8")

        os.environ["STUDYHELP_PROJECT"] = proj_name
        started = start_ingest(target_proj_dir, solve=(auto_solve != "0"), sniff=sniff)
        return {"ok": True, "project_id": proj_name, "title": profile.get("book", {}).get("title", proj_name),
                "ingest_started": started}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/ingest/resume")
def api_ingest_resume(req: Dict[str, Any]):
    """继续构建 / 开始解题：已切好的题不重切（保留核对过的切片与题干），已解出的题自动跳过。"""
    from core.ingest_job import start_ingest
    proj = (req or {}).get("project_id") or get_active_project_dir().name
    target = (DATA_ROOT / proj).resolve()
    if DATA_ROOT.resolve() not in target.parents or not target.is_dir() or not any(target.glob("*.pdf")):
        return JSONResponse(status_code=404, content={"ok": False, "error": f"项目不存在或没有 PDF: {proj}"})
    if PROBLEM_JOBS_RUNNING():
        return JSONResponse(status_code=409, content={"ok": False, "error": "有单题重解任务在运行，请稍候"})
    os.environ["STUDYHELP_PROJECT"] = proj
    if not start_ingest(target, solve=bool((req or {}).get("solve", True)), skip_crop=not bool((req or {}).get("recrop", False))):
        return JSONResponse(status_code=409, content={"ok": False, "error": "已有构建任务在运行"})
    return {"ok": True, "project_id": proj}

@app.get("/api/ingest/status")
def api_ingest_status():
    from core.ingest_job import INGEST
    return INGEST


class TimuImportRequest(BaseModel):
    text: str
    check: Optional[bool] = False
    project_id: Optional[str] = None


@app.get("/api/timu/prompt")
def api_timu_prompt():
    """交给大模型对话框的提示词：让它按 ### 题号 的格式输出全部题目"""
    from core.timu_import import PROMPT
    return {"prompt": PROMPT}


@app.post("/api/timu/import")
def api_timu_import(req: TimuImportRequest):
    """导入全部题目文本（已切题的书立即按题号写入；还没切完的书在切题后自动导入）"""
    from core.ingest_job import INGEST
    from core.timu_import import apply_import, parse_timu
    p_dir = (DATA_ROOT / req.project_id) if req.project_id else get_active_project_dir()
    if not p_dir.is_dir():
        return JSONResponse(status_code=404, content={"ok": False, "error": f"工作项不存在: {p_dir.name}"})
    parsed, _ = parse_timu(req.text)
    if not parsed:
        return JSONResponse(status_code=400, content={"ok": False, "error": "没有识别到题号标题（应为 ### 3.12 这样的行）"})
    # 还没切完题（构建中的 OCR 阶段，或还没开始）：先存下来，切完题后自动按题号导入
    if not any(p_dir.glob("Chapter_*/problems.json")):
        (p_dir / "timu_import.md").write_text(req.text, encoding="utf-8")
        (p_dir / "timu_import_pending.json").write_text(json.dumps({"check": bool(req.check)}), encoding="utf-8")
        return {"ok": True, "deferred": True, "parsed": len(parsed)}
    if INGEST.get("is_running") and INGEST.get("project") == p_dir.name:
        return JSONResponse(status_code=409, content={"ok": False, "error": "这本书已经切好题、正在转写/解题，等这一轮完成后再导入（导入会替换题干，已解的题需要重解）"})
    with _WRITE_LOCK:
        rep = apply_import(p_dir, req.text, check=bool(req.check))
    return {"ok": True, "report": rep}


@app.get("/api/timu/report")
def api_timu_report():
    rep = get_active_project_dir() / "timu_import_report.json"
    return json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else {}


@app.get("/review", response_class=HTMLResponse)
def review_page():
    """切题检查页：按题号列出每道题的切片，标出跨页拼接、插图归位等"""
    return HTMLResponse((TEMPLATES_DIR / "review.html").read_text(encoding="utf-8"))


@app.get("/api/review")
def api_review(project_id: Optional[str] = None):
    from PIL import Image as _Img
    p_dir = (DATA_ROOT / project_id) if project_id else get_active_project_dir()
    if DATA_ROOT.resolve() not in p_dir.resolve().parents or not p_dir.is_dir():
        return JSONResponse(status_code=404, content={"error": "工作项不存在"})
    rep = {}
    if (p_dir / "crop_report.json").exists():
        rep = json.loads((p_dir / "crop_report.json").read_text(encoding="utf-8"))
    attention = {}
    if (p_dir / "verify_report.json").exists():
        attention = json.loads((p_dir / "verify_report.json").read_text(encoding="utf-8")).get("needs_attention", {})
    names = {k: v.get("name", "") for k, v in load_book_config(p_dir).get("chapters", {}).items()}
    ai = {}
    if (p_dir / "crop_check_report.json").exists():
        ai = json.loads((p_dir / "crop_check_report.json").read_text(encoding="utf-8"))
    items = []
    page_info: Dict[str, Any] = {}   # 原页图：页码 -> {url, w, h}
    for pj in sorted(p_dir.glob("Chapter_*/problems.json")):
        ch = str(int(pj.parent.name.split("_")[1]))
        flagged = rep.get("chapters", {}).get(ch, {}).get("flagged", {})
        for prob in json.loads(pj.read_text(encoding="utf-8")):
            pid = str(prob["problem_id"])
            sp = pj.parent / "pages" / f"problem_{pid}_slice.png"
            w = h = 0
            if sp.exists():
                with _Img.open(sp) as im:
                    w, h = im.size
            pages = {}
            for b in prob.get("boxes") or []:
                pg = pj.parent / "pages" / f"page_{b[0]}.png"
                if pg.exists() and str(b[0]) not in page_info:
                    with _Img.open(pg) as im:
                        page_info[str(b[0])] = {"url": f"/data/{p_dir.name}/{pj.parent.name}/pages/{pg.name}", "w": im.size[0], "h": im.size[1]}
            items.append({
                "problem_id": pid, "chapter": ch, "chapter_name": names.get(ch, ""), "page": prob.get("page"),
                "boxes": prob.get("boxes") or [],
                "masks": prob.get("masks") or [],
                "ai": ai.get("results", {}).get(pid),
                "flags": flagged.get(pid, []), "w": w, "h": h,
                "issues": [x for x in attention.get(pid, []) if "切片" in x],
                "img": (f"/data/{p_dir.name}/{pj.parent.name}/pages/{sp.name}?v={int(sp.stat().st_mtime)}" if sp.exists() else ""),
            })
    # 跨页加区域需要相邻页：把已用页的下一页也放进来（页图不存在就从 PDF 渲染到第一个章节目录）
    try:
        from core.crop_edit import _page_png
        first_ch = next(iter(sorted(p_dir.glob("Chapter_*"))), None)
        import pymupdf
        pdf = next(x for x in sorted(p_dir.glob("*.pdf")) if not x.name.startswith(("Book_", "Chapter_")))
        with pymupdf.open(str(pdf)) as _doc:
            n_pages = len(_doc)
        used = sorted({int(k) for k in page_info})
        # 用到的每一页，前后各补一页（手动调整时可以把框拖到相邻页）
        for nxt in sorted({q for pg in used for q in (pg - 1, pg + 1)} - set(used)):
            if nxt < 1 or nxt > n_pages or first_ch is None:
                continue
            f = None
            for chd in sorted(p_dir.glob("Chapter_*")):
                if (chd / "pages" / f"page_{nxt}.png").exists():
                    f = chd / "pages" / f"page_{nxt}.png"
                    break
            f = f or _page_png(p_dir, first_ch, nxt, int(load_book_config(p_dir).get("book", {}).get("dpi", 150)))
            with _Img.open(f) as im:
                page_info[str(nxt)] = {"url": f"/data/{p_dir.name}/{f.parent.parent.name}/pages/{f.name}", "w": im.size[0], "h": im.size[1]}
    except Exception as e:
        print(f"[-] 相邻页补充失败: {e}")
    from core.ingest_job import INGEST as _ING
    projects = sorted(d.name for d in DATA_ROOT.iterdir() if d.is_dir() and (
        (d / "crop_report.json").exists() or (_ING.get("is_running") and _ING.get("project") == d.name)))
    from core.final_check import compute_notes
    return {"project": p_dir.name, "projects": projects, "items": items, "pages": page_info, "notes": compute_notes(p_dir),
            "ai_checked_at": ai.get("checked_at"), "ai_missing": ai.get("missing", [])}


class BoxesRequest(BaseModel):
    pid: str
    boxes: List[List[int]]
    masks: Optional[List[List[int]]] = None
    project_id: Optional[str] = None


@app.post("/api/review/boxes")
def api_review_save_boxes(req: BoxesRequest):
    """切题检查页：保存人工调整的框，按新框从原页重新切出切片"""
    from core.crop_edit import save_boxes
    p_dir = (DATA_ROOT / req.project_id) if req.project_id else get_active_project_dir()
    if DATA_ROOT.resolve() not in p_dir.resolve().parents:
        return JSONResponse(status_code=404, content={"ok": False, "error": "工作项不存在"})
    try:
        with _WRITE_LOCK:
            r = save_boxes(p_dir, req.pid, req.boxes, masks=req.masks, dpi=int(load_book_config(p_dir).get("book", {}).get("dpi", 150)))
        return {"ok": True, **r}
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.get("/api/cropcheck/estimate")
def api_cropcheck_estimate():
    from core.crop_checker import estimate
    return dict(estimate(get_active_project_dir()), project_id=get_active_project_dir().name)


@app.post("/api/cropcheck/start")
def api_cropcheck_start():
    """AI 辅助检查切题（后台逐页运行）"""
    from core.crop_checker import start_check, estimate
    from core.ingest_job import INGEST
    p_dir = get_active_project_dir()
    if not estimate(p_dir)["has_boxes"]:
        return JSONResponse(status_code=400, content={"ok": False, "error": "这本书没有记录切割位置，请先重新切题（继续构建）"})
    if INGEST.get("is_running") and INGEST.get("project") == p_dir.name:
        return JSONResponse(status_code=409, content={"ok": False, "error": "这本书正在构建，完成后再检查"})
    if not start_check(p_dir):
        return JSONResponse(status_code=409, content={"ok": False, "error": "已有一个切题检查在运行"})
    return {"ok": True}


@app.post("/api/cropfix/start")
def api_cropfix_start(req: Dict[str, Any] = None):
    """AI 一键修复：针对 AI 检查标出的题给出修正并自动落到坐标，修完复查"""
    from core.crop_fixer import start_fix
    from core.crop_checker import CROPCHECK
    req = req or {}
    p_dir = (DATA_ROOT / req["project_id"]) if req.get("project_id") else get_active_project_dir()
    if not (p_dir / "crop_check_report.json").exists():
        return JSONResponse(status_code=400, content={"ok": False, "error": "请先做一次 AI 切题检查"})
    if CROPCHECK.get("is_running"):
        return JSONResponse(status_code=409, content={"ok": False, "error": "AI 切题检查正在运行，稍后再修复"})
    if not start_fix(p_dir, req.get("pids")):
        return JSONResponse(status_code=409, content={"ok": False, "error": "已有修复任务在运行"})
    return {"ok": True}


@app.get("/api/cropfix/status")
def api_cropfix_status():
    from core.crop_fixer import CROPFIX
    return CROPFIX


@app.get("/api/cropcheck/status")
def api_cropcheck_status():
    from core.crop_checker import CROPCHECK
    return CROPCHECK


@app.get("/api/exercise-pdf")
def api_exercise_pdf():
    """切题后拆出“只含习题页”的 PDF（全部 + 每章），供上传到大模型对话框转写题目"""
    from core.exercise_pdf import build
    p_dir = get_active_project_dir()
    try:
        r = build(p_dir)
    except Exception as e:
        return JSONResponse(status_code=500, content={"ready": False, "error": str(e)})
    r["project_id"] = p_dir.name
    return r


@app.get("/api/exercise-pdf/file")
def api_exercise_pdf_file(name: str):
    from core.exercise_pdf import file_path
    f = file_path(get_active_project_dir(), name)
    if not f:
        return JSONResponse(status_code=404, content={"error": "文件不存在"})
    return FileResponse(str(f), media_type="application/pdf", filename=f.name)


@app.post("/api/exercise-pdf/open")
def api_exercise_pdf_open():
    """在资源管理器里打开“习题页”文件夹"""
    from core.exercise_pdf import build, DIR_NAME
    p_dir = get_active_project_dir()
    build(p_dir)
    target = p_dir / DIR_NAME
    try:
        os.startfile(str(target))
        return {"ok": True, "path": str(target)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/usage")
def api_usage(project_id: Optional[str] = None):
    """本书真实 token 用量（每次模型调用记账汇总）；旧项目没有记录时 recorded=False。"""
    from core.usage import summarize
    p_dir = (DATA_ROOT / project_id) if project_id else get_active_project_dir()
    return dict(summarize(p_dir), project_id=p_dir.name)


@app.get("/api/profile")
def get_profile():
    p_dir = get_active_project_dir()
    cfg = load_book_config(p_dir)
    meta = get_book_metadata(cfg)
    chapters = get_all_chapters(cfg)
    return {
        **meta,
        "project_name": p_dir.name,
        "chapters": chapters
    }

def clean_solution_content(content: str) -> str:
    """
    剔除解答中的冗余「题目重述」段落与前后悬挂的分割线。
    原题文本与原书切片已统一由左侧原题栏全要素沉浸呈现，右侧专注于纯粹、优雅的高浓度分步推导。
    """
    if not content:
        return content
    pattern = r'###\s*(?:[📌📝]\s*)?题目重述[^\n]*\n+(.*?)(?=\n+---|\n+###|\Z)'
    content = re.sub(pattern, '', content, flags=re.DOTALL)
    content = re.sub(r'---\s*\n+(\s*---\s*\n+)+', '---\n\n', content)
    content = re.sub(r'^(##\s*[^\n]+\n+)\s*---\s*\n+', r'\1', content)
    return content.strip()

def embed_image_replace_restatement(content: str, img_url: str) -> str:
    """阶段一/阶段三：把解答里的「题目重述」换成原书题图（与 test1 奥本海默工作台一致）；没有题目重述则在开头插入原图。"""
    if not content:
        return content
    img_html = ""
    if img_url:
        img_html = ("### 📷 原书真实高清题图\n\n<div class=\"my-2 flex justify-center\"><img src=\"" + img_url +
                    "\" alt=\"原题切片\" class=\"max-w-full rounded-lg border border-slate-200 shadow-2xs\" /></div>")
    pattern = r'###\s*(?:[📌📝]\s*)?题目重述[^\n]*\n+(.*?)(?=\n+---|\n+###|\Z)'
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, lambda m: img_html + "\n\n", content, count=1, flags=re.DOTALL)
    elif img_html:
        m_head = re.search(r'^(##\s*习题\s*[^\n]+\n+)', content)
        if m_head:
            content = content[:m_head.end()] + img_html + "\n\n---\n\n" + content[m_head.end():]
        else:
            content = img_html + "\n\n---\n\n" + content
    content = re.sub(r'---\s*\n+(\s*---\s*\n+)+', '---\n\n', content)
    return content.strip()


def format_compact_with_text(compact_content: str, problem_text: str) -> str:
    if not compact_content:
        return compact_content
    if problem_text and problem_text.strip():
        snippet = problem_text.strip()[:30]
        if snippet not in compact_content:
            text_block = f"### 📝 题目文本\n\n{problem_text.strip()}\n\n---\n\n"
            m_head = re.search(r'^(###?\s*习题\s*[^\n]+\n+)', compact_content)
            if m_head:
                compact_content = compact_content[:m_head.end()] + text_block + compact_content[m_head.end():]
            else:
                compact_content = text_block + compact_content
    compact_content = re.sub(r'---\s*\n+(\s*---\s*\n+)+', '---\n\n', compact_content)
    return compact_content

@app.get("/api/chapter/{ch_id}/problems")
@app.get("/api/chapters/{ch_id}/problems")
def get_chapter_problems(ch_id: str):
    ch_idx = int(ch_id) if ch_id.isdigit() else 1
    p_dir = get_active_project_dir()
    ch_dir = p_dir / f"Chapter_{ch_idx:02d}"
    p_json = ch_dir / "problems.json"
    if p_json.exists():
        try:
            probs = json.loads(p_json.read_text(encoding="utf-8"))
        except Exception:
            return []
        # 附上每题的评审状态，供左侧列表“只看待复核”筛选与角标
        attention = {}
        rep = p_dir / "verify_report.json"
        if rep.exists():
            try:
                attention = json.loads(rep.read_text(encoding="utf-8")).get("needs_attention", {})
            except Exception:
                pass
        from core.crop_checker import issues_for
        for k, v in issues_for(p_dir).items():
            attention[k] = list(attention.get(k, [])) + v
        for p in probs:
            pid = str(p.get("problem_id"))
            sf = ch_dir / "slots" / f"slot_{pid}.json"
            p["stale_text"] = False
            if sf.exists():
                try:
                    s = json.loads(sf.read_text(encoding="utf-8"))
                    p["status"], p["score"] = s.get("status") or "passed", s.get("score", "")
                    p["stale_text"] = bool(s.get("stale_text"))
                except Exception:
                    p["status"] = "unreadable"
            else:
                p["status"] = "unsolved"
            p["issues"] = attention.get(pid, [])
            p["needs_review"] = bool(p["issues"]) or p["stale_text"] or p["status"] not in ("passed", "unsolved")
            p["job"] = PROBLEM_JOBS.get(f"{p_dir.name}/{pid}", {}).get("state")
        return probs
    return []

def _text_source_for(ch_dir: Path, pid: str) -> str:
    try:
        for p in json.loads((ch_dir / "problems.json").read_text(encoding="utf-8")):
            if str(p.get("problem_id")) == str(pid):
                return p.get("text_source", "")
    except Exception:
        pass
    return ""


def _attention_for(p_dir: Path, pid: str) -> List[str]:
    from core.crop_checker import issues_for
    rep = p_dir / "verify_report.json"
    out: List[str] = []
    try:
        out = json.loads(rep.read_text(encoding="utf-8")).get("needs_attention", {}).get(str(pid), []) if rep.exists() else []
    except Exception:
        pass
    return out + issues_for(p_dir).get(str(pid), [])


@app.get("/api/chapter/{ch_id}/problem/{pid}")
@app.get("/api/chapters/{ch_id}/problem/{pid}")
def get_single_problem_detail(ch_id: str, pid: str):
    ch_idx = int(ch_id) if ch_id.isdigit() else 1
    p_dir = get_active_project_dir()
    ch_dir = p_dir / f"Chapter_{ch_idx:02d}"
    
    # 查找专属切片
    slice_file = ch_dir / "pages" / f"problem_{pid}_slice.png"
    page_img_url = f"/chapters/Chapter_{ch_idx:02d}/pages/problem_{pid}_slice.png?t={int(time.time())}" if slice_file.exists() else ""

    # 读取原题题干（优先从 timu/ 目录下读取高质量排版原题，杜绝编码乱码）
    problem_text = ""
    timu_file = ch_dir / "timu" / f"timu_{pid}.md"
    if timu_file.exists():
        try:
            c = timu_file.read_text(encoding="utf-8")
            m_text = re.search(r'##\s*📝\s*题目文本与公式\s*\n\n(.*?)(?=\n\n#|\Z)', c, re.DOTALL)
            if m_text and m_text.group(1).strip():
                problem_text = m_text.group(1).strip()
            if not page_img_url:
                m_img = re.search(r'!\[.*?\]\(\.\./pages/([^)]+)\)', c)
                if m_img:
                    page_img_url = f"/chapters/Chapter_{ch_idx:02d}/pages/{m_img.group(1)}"
        except Exception:
            pass

    if not problem_text:
        p_json = ch_dir / "problems.json"
        if p_json.exists():
            try:
                probs = json.loads(p_json.read_text(encoding="utf-8"))
                for p in probs:
                    if str(p.get("problem_id")) == str(pid):
                        problem_text = p.get("text", "")
                        break
            except Exception:
                pass

    # 读取 slot 求解定稿
    slot_file = ch_dir / "slots" / f"slot_{pid}.json"
    # problems.json 暂无题干（如续跑重写期间）时，回退到解答里保存的题干，保证原题栏和各阶段嵌入的题目不会变空
    if len((problem_text or "").strip()) < 8 and slot_file.exists():
        try:
            problem_text = json.loads(slot_file.read_text(encoding="utf-8")).get("text") or problem_text
        except Exception:
            pass
    if slot_file.exists():
        try:
            slot_data = json.loads(slot_file.read_text(encoding="utf-8"))
            draft_content = embed_image_replace_restatement(slot_data.get("draft") or slot_data.get("stage1") or "", page_img_url)
            review_content = slot_data.get("review") or slot_data.get("stage2") or ""
            final_content = embed_image_replace_restatement(slot_data.get("final") or slot_data.get("stage3") or slot_data.get("stage3_deduction") or "", page_img_url)
            compact_content = format_compact_with_text(slot_data.get("compact") or slot_data.get("compact_breakdown") or "", problem_text)

            return {
                "problem_id": pid,
                "found": True,
                "problem_text": problem_text,
                "page_img_url": page_img_url,
                "draft": draft_content,
                "review": review_content,
                "final": final_content,
                "compact": compact_content,
                "final_answer": slot_data.get("final_answer", ""),
                "score": slot_data.get("score", ""),
                "status": slot_data.get("status", ""),
                "stale_text": bool(slot_data.get("stale_text")),
                "final_mode": slot_data.get("final_mode", ""),
                "usage": slot_data.get("usage"),
                "issues": _attention_for(p_dir, pid),
                "text_source": _text_source_for(ch_dir, pid),
            }
        except Exception:
            pass

    return {
        "problem_id": pid,
        "found": False,
        "problem_text": problem_text,
        "page_img_url": page_img_url,
        "draft": "",
        "review": "",
        "final": "",
        "compact": ""
    }

import threading

CHAPTER_SOLVER_TASKS: Dict[str, Dict[str, Any]] = {}
GLOBAL_SOLVER_TASK: Dict[str, Any] = {
    "is_running": False,
    "stop_event": None,
    "thread": None,
    "start_time": 0,
    "current_chapter": 1,
    "active_workers": {},
    "last_error": None
}

def get_all_chapters_stats(p_dir: Path):
    ch_dirs = sorted([d for d in p_dir.glob("Chapter_*") if d.is_dir() and (d / "problems.json").exists()])
    all_total = 0
    all_completed = 0
    chapters_summary = {}
    for ch_d in ch_dirs:
        m = re.search(r'Chapter_(\d+)', ch_d.name)
        if not m:
            continue
        c_idx = int(m.group(1))
        p_json = ch_d / "problems.json"
        c_total = 0
        if p_json.exists():
            try:
                c_total = len(json.loads(p_json.read_text(encoding="utf-8")))
            except Exception:
                pass
        slots_dir = ch_d / "slots"
        c_completed = len(list(slots_dir.glob("slot_*.json"))) if slots_dir.exists() else 0
        all_total += c_total
        all_completed += c_completed
        chapters_summary[c_idx] = {
            "total": c_total,
            "completed": c_completed,
            "is_done": c_total > 0 and c_completed >= c_total
        }
    return all_total, all_completed, chapters_summary

def _run_solve_all_bg(p_dir: Path, cfg: Dict[str, Any], stop_event: threading.Event):
    from core.solver import solve_all_book
    GLOBAL_SOLVER_TASK["is_running"] = True
    GLOBAL_SOLVER_TASK["last_error"] = None
    try:
        def _on_start(pid):
            GLOBAL_SOLVER_TASK["active_workers"][str(pid)] = {"problem": str(pid), "status": "求解中"}

        def _on_progress(pid, ok):
            GLOBAL_SOLVER_TASK["active_workers"].pop(str(pid), None)

        def _on_chapter_change(c_idx):
            GLOBAL_SOLVER_TASK["current_chapter"] = c_idx

        solve_all_book(
            concurrency=20,
            force=False,
            cfg=cfg,
            project_dir=p_dir,
            stop_flag=lambda: stop_event.is_set(),
            on_start=_on_start,
            on_progress=_on_progress,
            on_chapter_change=_on_chapter_change
        )
    except Exception as e:
        GLOBAL_SOLVER_TASK["last_error"] = str(e)
        print(f"[-] 全卷后台求解异常: {e}")
    finally:
        GLOBAL_SOLVER_TASK["is_running"] = False
        GLOBAL_SOLVER_TASK["active_workers"].clear()

def _run_chapter_solve_bg(p_dir: Path, ch_idx: int, cfg: Dict[str, Any], stop_event: threading.Event, task_info: Dict[str, Any]):
    from core.solver import solve_chapter
    from core.aggregator import aggregate_chapter, aggregate_all_book
    try:
        task_info["is_running"] = True
        solve_chapter(
            ch_idx,
            concurrency=20,
            force=False,
            cfg=cfg,
            project_dir=p_dir,
            stop_flag=lambda: stop_event.is_set(),
            on_progress=lambda pid, ok: None
        )
        aggregate_chapter(ch_idx, cfg, project_dir=p_dir)
        aggregate_all_book(p_dir)
    except Exception as e:
        print(f"[-] 章节 {ch_idx} 后台求解异常: {e}")
    finally:
        task_info["is_running"] = False

PROBLEM_JOBS: Dict[str, Dict[str, Any]] = {}   # "<工作项>/<题号>" -> {state, message, ...}
_WRITE_LOCK = threading.Lock()                 # 串行化 problems.json / 合订本 / PDF 的写入
_PDF_DIRTY: Dict[str, bool] = {}


def PROBLEM_JOBS_RUNNING(project: Optional[str] = None) -> bool:
    return any(j.get("state") in ("queued", "solving", "checking", "pdf") and (project is None or k.startswith(project + "/"))
               for k, j in PROBLEM_JOBS.items())


def _resolve_problem_bg(p_dir: Path, ch_idx: int, prob: Dict[str, Any], retranscribe: bool):
    from core.solver import init_gemini_client, solve_single_problem
    from core.aggregator import aggregate_chapter, aggregate_all_book
    from core.verifier import verify_project
    pid = str(prob["problem_id"])
    job = PROBLEM_JOBS[f"{p_dir.name}/{pid}"]
    try:
        cfg = load_book_config(p_dir)
        ch_dir = p_dir / f"Chapter_{ch_idx:02d}"
        (ch_dir / "slots").mkdir(parents=True, exist_ok=True)
        prob = dict(prob, section_idx=ch_idx,
                    section_name=prob.get("section_name") or cfg.get("chapters", {}).get(str(ch_idx), {}).get("name", ""))
        if retranscribe:
            # 题干本身有错：清掉旧题干，按原图重新转写并逐符号核对，再用新题干求解
            job.update(state="checking", message="按原图重新转写题干…")
            with _WRITE_LOCK:
                pj = ch_dir / "problems.json"
                probs = json.loads(pj.read_text(encoding="utf-8"))
                for p in probs:
                    if str(p.get("problem_id")) == pid:
                        p["text"], p["text_source"] = "", "none"
                pj.write_text(json.dumps(probs, ensure_ascii=False, indent=2), encoding="utf-8")
                sf = ch_dir / "slots" / f"slot_{pid}.json"
                if sf.exists():  # slot 里的旧题干也清掉，免得被当作回退题干
                    s = json.loads(sf.read_text(encoding="utf-8"))
                    s["text"] = ""
                    sf.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
                verify_project(p_dir, cfg, concurrency=1, check_solutions=False, only={pid})
                fresh = next((p for p in json.loads(pj.read_text(encoding="utf-8")) if str(p.get("problem_id")) == pid), None)
            if fresh:
                prob.update(text=fresh.get("text", ""), text_source=fresh.get("text_source", "none"))
        job.update(state="solving", message="求解中（草稿 → 评审 → 定稿）…")
        res = solve_single_problem(init_gemini_client(cfg), cfg, prob, ch_dir / "pages", force=True)
        (ch_dir / "slots" / f"slot_{pid}.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        job.update(state="checking", message="检验中…", score=res.get("score"), status=res.get("status"))
        with _WRITE_LOCK:
            verify_project(p_dir, cfg, concurrency=1, only={pid})
            aggregate_chapter(ch_idx, cfg, project_dir=p_dir)
            aggregate_all_book(p_dir)
            _PDF_DIRTY[p_dir.name] = True
        # PDF 编译较慢：多题连续重解时只在最后编一次
        job.update(state="pdf", message="重新生成 PDF…")
        with _WRITE_LOCK:
            if _PDF_DIRTY.pop(p_dir.name, False):
                from core.pdf_compiler import compile_all_suite_pdfs
                try:
                    compile_all_suite_pdfs(p_dir)
                except Exception as e:
                    print(f"[-] 重解后 PDF 编译失败: {e}")
        job.update(state="done", message=f"已重解（评分 {res.get('score') or '无'}）", finished=time.time())
    except Exception as e:
        import traceback
        traceback.print_exc()
        job.update(state="error", message=f"重解失败：{e}", finished=time.time())


@app.post("/api/chapter/{ch_id}/solve-single/{pid}")
def api_solve_single_problem(ch_id: str, pid: str, retranscribe: int = 0):
    """重解这一题（后台运行，立即返回）：求解 -> 检验 -> 刷新合订本与 PDF。
    retranscribe=1 时先按原图重新转写题干（题干本身有错时用）。进度查 /api/problem-jobs。"""
    from core.ingest_job import INGEST
    ch_idx = int(ch_id) if ch_id.isdigit() else 1
    p_dir = get_active_project_dir()
    ch_dir = p_dir / f"Chapter_{ch_idx:02d}"
    p_json = ch_dir / "problems.json"
    if not p_json.exists():
        return JSONResponse(status_code=404, content={"ok": False, "error": f"Chapter_{ch_idx:02d}/problems.json 未找到"})
    probs = json.loads(p_json.read_text(encoding="utf-8"))
    target_prob = next((p for p in probs if str(p.get("problem_id")) == str(pid)), None)
    if not target_prob:
        return JSONResponse(status_code=404, content={"ok": False, "error": f"题目 {pid} 未在 Chapter {ch_idx} 中找到"})
    if INGEST.get("is_running") and INGEST.get("project") == p_dir.name:
        return JSONResponse(status_code=409, content={"ok": False, "error": "这本书正在整体构建，完成后再重解单题"})
    key = f"{p_dir.name}/{pid}"
    if PROBLEM_JOBS.get(key, {}).get("state") in ("queued", "solving", "checking", "pdf"):
        return {"ok": True, "already_running": True, "job": PROBLEM_JOBS[key]}
    PROBLEM_JOBS[key] = {"project": p_dir.name, "chapter": ch_idx, "pid": str(pid), "state": "queued",
                         "message": "排队中…", "started": time.time()}
    threading.Thread(target=_resolve_problem_bg, args=(p_dir, ch_idx, target_prob, bool(retranscribe)), daemon=True).start()
    return {"ok": True, "job": PROBLEM_JOBS[key]}


@app.get("/api/problem-jobs")
def api_problem_jobs():
    p = get_active_project_dir().name
    return {k.split("/", 1)[1]: v for k, v in PROBLEM_JOBS.items() if k.startswith(p + "/")}

@app.post("/api/solve/start-all")
@app.post("/api/solve-all")
@app.post("/api/solve/start")
def api_start_solve_all():
    """启动全书/全卷全量自动化贯通求解"""
    if GLOBAL_SOLVER_TASK.get("is_running"):
        return {"ok": True, "message": "全卷自动化求解流水线已在运行中"}
    
    p_dir = get_active_project_dir()
    cfg = load_book_config(p_dir)
    stop_event = threading.Event()
    GLOBAL_SOLVER_TASK["stop_event"] = stop_event
    GLOBAL_SOLVER_TASK["start_time"] = time.time()
    GLOBAL_SOLVER_TASK["active_workers"] = {}
    GLOBAL_SOLVER_TASK["is_running"] = True
    
    th = threading.Thread(
        target=_run_solve_all_bg,
        args=(p_dir, cfg, stop_event),
        daemon=True
    )
    GLOBAL_SOLVER_TASK["thread"] = th
    th.start()
    return {"ok": True, "message": "全卷全量自动化解题流水线已成功启动"}

@app.post("/api/solve/pause-all")
@app.post("/api/solve-all/pause")
@app.post("/api/solve/pause")
def api_pause_solve_all():
    """暂停全书/全卷全量求解"""
    if GLOBAL_SOLVER_TASK.get("stop_event"):
        GLOBAL_SOLVER_TASK["stop_event"].set()
    GLOBAL_SOLVER_TASK["is_running"] = False
    return {"ok": True, "message": "全卷求解已下达暂停指令"}

@app.post("/api/solve/restart-all")
@app.post("/api/solve-all/restart")
def api_restart_solve_all():
    """清空全卷所有题解缓存并重新从第一题开始全量解题"""
    if GLOBAL_SOLVER_TASK.get("stop_event"):
        GLOBAL_SOLVER_TASK["stop_event"].set()
    GLOBAL_SOLVER_TASK["is_running"] = False
    time.sleep(0.5)

    p_dir = get_active_project_dir()
    for ch_d in p_dir.glob("Chapter_*"):
        s_dir = ch_d / "slots"
        if s_dir.exists():
            for sf in s_dir.glob("slot_*.json"):
                try:
                    sf.unlink()
                except Exception:
                    pass
    return api_start_solve_all()

@app.get("/api/solve/status")
def get_global_solve_status():
    p_dir = get_active_project_dir()
    all_total, all_completed, chapters_summary = get_all_chapters_stats(p_dir)
    is_running = GLOBAL_SOLVER_TASK.get("is_running", False)
    start_time = GLOBAL_SOLVER_TASK.get("start_time", 0)
    elapsed = round(time.time() - start_time, 1) if is_running and start_time > 0 else 0
    from core.usage import summarize
    tokens = summarize(p_dir).get("total", 0)  # 真实用量（每次模型调用记账）

    return {
        "is_running": is_running,
        "all_total": all_total,
        "all_completed": all_completed,
        "pct": round(all_completed / all_total * 100) if all_total > 0 else 0,
        "total_tokens_used": tokens,
        "total_elapsed_time_s": elapsed,
        "active_workers": GLOBAL_SOLVER_TASK.get("active_workers", {}),
        "current_chapter": GLOBAL_SOLVER_TASK.get("current_chapter", 1),
        "chapters_summary": chapters_summary
    }

@app.post("/api/chapter/{ch_id}/start")
@app.post("/api/ch2/start")
def api_start_chapter_solve(ch_id: str = "1"):
    """兼容旧接口：启动全卷做题"""
    return api_start_solve_all()

@app.post("/api/chapter/{ch_id}/pause")
@app.post("/api/ch2/pause")
def api_pause_chapter_solve(ch_id: str = "1"):
    """兼容旧接口：暂停全卷做题"""
    return api_pause_solve_all()

@app.post("/api/chapter/{ch_id}/restart")
@app.post("/api/ch2/restart")
def api_restart_chapter_solve(ch_id: str = "1"):
    """兼容旧接口：清空重跑"""
    return api_restart_solve_all()

@app.get("/api/chapter/{ch_id}/status")
def get_chapter_status(ch_id: str):
    ch_idx = int(ch_id) if ch_id.isdigit() else 1
    p_dir = get_active_project_dir()
    ch_dir = p_dir / f"Chapter_{ch_idx:02d}"
    p_json = ch_dir / "problems.json"
    total = 0
    if p_json.exists():
        try:
            total = len(json.loads(p_json.read_text(encoding="utf-8")))
        except Exception:
            pass
    slots_dir = ch_dir / "slots"
    completed = len(list(slots_dir.glob("slot_*.json"))) if slots_dir.exists() else 0
    completed_list = [f.stem.replace("slot_", "") for f in slots_dir.glob("slot_*.json")] if slots_dir.exists() else []

    all_total, all_completed, chapters_summary = get_all_chapters_stats(p_dir)
    global_is_running = GLOBAL_SOLVER_TASK.get("is_running", False)

    start_time = GLOBAL_SOLVER_TASK.get("start_time", 0)
    elapsed = round(time.time() - start_time, 1) if global_is_running and start_time > 0 else 0
    from core.usage import summarize
    tokens = summarize(p_dir).get("total", 0)  # 真实用量（每次模型调用记账）

    active_workers = GLOBAL_SOLVER_TASK.get("active_workers", {})
    if not active_workers and global_is_running:
        for i in range(min(4, max(1, all_total - all_completed))):
            active_workers[f"Worker-{i+1}"] = {
                "problem": f"{ch_idx}.{completed + i + 1}",
                "status": "solving"
            }

    return {
        "chapter": ch_idx,
        "is_running": global_is_running,
        "total_problems": total,
        "completed_count": completed,
        "completed_list": completed_list,
        "total_tokens_used": tokens,
        "total_elapsed_time_s": elapsed,
        "active_workers": active_workers,
        # 全卷统计数据
        "all_total": all_total,
        "all_completed": all_completed,
        "all_pct": round(all_completed / all_total * 100) if all_total > 0 else 0,
        "current_solving_chapter": GLOBAL_SOLVER_TASK.get("current_chapter", ch_idx),
        "chapters_summary": chapters_summary
    }

class SolveRequest(BaseModel):
    chapter_id: int
    concurrency: Optional[int] = 20

@app.post("/api/solve")
def run_solver(req: SolveRequest):
    p_dir = get_active_project_dir()
    cfg = load_book_config(p_dir)
    res = solve_chapter(req.chapter_id, concurrency=req.concurrency, cfg=cfg, project_dir=p_dir)
    return res

class ChatRequest(BaseModel):
    problem_id: str
    message: str
    context: Optional[str] = ""

@app.post("/api/tutor/chat")
def tutor_chat(req: ChatRequest):
    try:
        client = init_gemini_client()
        p_dir = get_active_project_dir()
        cfg = load_book_config(p_dir)
        meta = get_book_metadata(cfg)
        sys_prompts = get_system_prompts(meta.get("subject", "math"))
        
        prompt = f"""{sys_prompts['tutor']}

当前上下文环境：
- 所属课程/试卷: {meta['title']}
- 当前题目题号: {req.problem_id}
- 题目及题解背景:
{req.context}

学生提问:
{req.message}

请以启发式教学名师的口吻，由浅入深解答学生的疑问。公式请使用规范 LaTeX 语法输出。"""

        response = client.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt,
        )
        from core import usage
        usage.record("tutor", getattr(response, "usage_metadata", None), "gemini-3.8-flash", project_dir=p_dir)
        return {"reply": response.text}
    except Exception as e:
        return {"reply": f"伴读助手遇到网络或解析问题: {str(e)}"}

@app.post("/api/open/vscode")
@app.post("/api/open-vscode")
@app.post("/api/open/vscode")
async def open_in_vscode_route(
    request: Request,
    chapter: Optional[int] = 1,
    type: Optional[str] = "all"
):
    # 支持 query param 与 JSON body 混合兼容
    p_dir = get_active_project_dir()
    ch_val = chapter
    type_val = type
    try:
        body = await request.json()
        if isinstance(body, dict):
            if "chapter" in body or "chapter_id" in body:
                raw_ch = body.get("chapter") or body.get("chapter_id")
                ch_val = int(raw_ch) if str(raw_ch).isdigit() else 1
            if "type" in body or "file_type" in body:
                type_val = body.get("type") or body.get("file_type")
    except Exception:
        pass

    ch_dir = p_dir / f"Chapter_{ch_val:02d}"
    if type_val == "dir":
        target = ch_dir if ch_dir.exists() else p_dir
        os.startfile(str(target))
        return {"ok": True, "target": target.name, "path": str(target)}
    elif type_val in ("pdf", "print_pdf"):
        target = ch_dir / f"Chapter_{ch_val:02d}_Print.pdf"
        if not target.exists():
            target = p_dir / "Book_Print.pdf"
        if target.exists():
            os.startfile(str(target))
            return {"ok": True, "target": target.name, "path": str(target)}
        return {"ok": False, "error": f"PDF 文件尚未生成: {target.name}"}
    elif type_val == "print":
        target = ch_dir / f"Chapter_{ch_val:02d}_Print.md"
        if not target.exists():
            target = p_dir / "Book_Print.md"
    elif type_val == "compact":
        target = ch_dir / f"Chapter_{ch_val:02d}_Compact.md"
        if not target.exists():
            target = p_dir / "Book_Compact.md"
    else:
        target = ch_dir / f"Chapter_{ch_val:02d}_All.md"
        if not target.exists():
            target = p_dir / "Book_All.md"

    if not target.exists():
        return {"ok": False, "error": f"目标尚未生成: {target.name}"}

    try:
        if VSCODE_PATH.exists():
            subprocess.Popen([str(VSCODE_PATH), str(target)])
        else:
            os.startfile(str(target))
        return {"ok": True, "target": target.name, "path": str(target)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/open-book")
@app.post("/api/open/pdf")
@app.post("/api/open-book-resource")
async def open_book_file_route(
    request: Request,
    type: Optional[str] = "all_pdf"
):
    # 支持 query param 与 JSON body 混合兼容
    type_val = type
    try:
        body = await request.json()
        if isinstance(body, dict):
            if "type" in body or "resource" in body:
                type_val = body.get("type") or body.get("resource")
    except Exception:
        pass

    p_dir = get_active_project_dir()
    mapping = {
        "print_pdf": p_dir / "Book_Print.pdf",
        "print_md": p_dir / "Book_Print.md",
        "timu_pdf": p_dir / "Book_Timu_All.pdf",
        "timu_md": p_dir / "Book_Timu_All.md",
        "all_pdf": p_dir / "Book_All.pdf",
        "all_md": p_dir / "Book_All.md"
    }
    target = mapping.get(type_val, p_dir / "Book_All.pdf")
    if not target.exists():
        return {"ok": False, "error": f"全书文件尚未生成: {target.name}"}

    try:
        if target.suffix.lower() == ".pdf":
            try:
                os.startfile(str(target))
                return {"ok": True, "target": target.name, "path": str(target)}
            except Exception:
                if VSCODE_PATH.exists():
                    subprocess.Popen([str(VSCODE_PATH), str(target)])
                return {"ok": True, "target": target.name, "path": str(target)}
        else:
            if VSCODE_PATH.exists():
                subprocess.Popen([str(VSCODE_PATH), str(target)])
            else:
                os.startfile(str(target))
            return {"ok": True, "target": target.name, "path": str(target)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def start_server(port: int = 8089):
    print(f"============================================================")
    print(f"StudyHelp 通用题库工作台启动中...")
    print(f"活跃工程数据: {get_active_project_dir().name}")
    print(f"本地访问地址: http://127.0.0.1:{port}")
    print(f"============================================================")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

if __name__ == "__main__":
    start_server(8089)
