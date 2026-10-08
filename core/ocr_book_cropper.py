# -*- coding: utf-8 -*-
"""
StudyHelp 教材习题通用切题引擎 (OCR Book Cropper)

面向“扫描版教材”：整页 OCR 得到每一行文字的坐标，再用“章-序号”连续性规则识别题号锚点，
按锚点做几何切割，跨页自动拼接。若 PDF 自带文字层则直接读文字层，不做 OCR。

识别原则（与具体教材无关）：
1. 习题区域由标题行触发（习题 / 思考题 / 基本题 / 深入题 / Problems ...），遇到章首页或“答案/附录”标题即关闭；
2. 题号只接受“期望的下一个序号”（章.序号 / 章-序号 / 章—序号），书中其它数字（小数、图号、节号）因序号不连续被拒绝；
3. 漏识别 1~2 个题号时，允许跳号并在报告中标记 gap，不再静默吞题；
4. 切图上下边界取自题号行位置与墨迹范围，页眉页脚由横线/文字位置自动检测，不使用固定像素。

输出（与 test2 其它模块约定一致）：
  <out_root>/Chapter_XX/pages/problem_X.Y_slice.png
  <out_root>/Chapter_XX/pages/page_N.png   (题目涉及的整页图)
  <out_root>/Chapter_XX/problems.json
  <out_root>/crop_report.json              (缺号、跨页、低置信度等质检结果)
"""

import json
import os
import re
import sys

# 多进程并行 OCR 时必须把数学库线程压到 1，否则每进程 ~60 个自旋线程互相抢核，吞吐暴跌
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pymupdf
from PIL import Image

# ---------------------------------------------------------------- 规则常量
_DIGIT_FIX = str.maketrans({"l": "1", "I": "1", "|": "1", "O": "0", "o": "0"})
# 题号可带“习题”前缀（“习题1-1”“习题 2.3”）和难题记号（“△习题2-13”）；“仿真题1-3”“思考题1-2”等另一套编号不算
ANCHOR_RE = re.compile(r"^\s*[△▲Δ*＊☆★]?\s*(?:习\s*题\s*)?([0-9lIO|]{1,2})\s*[-－—–﹣一_.．·]\s*([0-9lIO|]{1,4})")
# 单独成行的插图图注：“图 P3.45”“图P3-12”“图 3.12(a)”（正文里“由图P3.19所示…”不算）
# 单独成行的小标题（“习题与思考题”“Multisim仿真练习题”“深入题”…）和仿真题区：上一题到此为止
TITLE_RE = re.compile(r"^[\W\d_]*(?:Mul[a-zA-Z]*)?\s*(?:仿真)?(?:习题与思考题|思考题与习题|思考题和习题|习题|思考题|练习题|复习题|"
                      r"基本题|深入题|扩充题|Problems|Exercises)[\W\d_]*$", re.I)
SIM_RE = re.compile(r"^\s*(?:Mul[a-zA-Z]*\s*仿真|Multisim|仿真\s*题|仿真练习)", re.I)
PREFIX_RE = re.compile(r"^\s*[△▲Δ*＊☆★]?\s*习\s*题\s*[0-9lIO|]")
CAPTION_RE = re.compile(r"^图\s*[PpＰ]?\s*(\d{1,2})\s*[.\-－—–·．]\s*(\d{1,3})\s*(?:\([a-z]\)|（[a-z]）)?\s*$")
HEADING_RE = re.compile(r"(习题|思考题|练习题|复习题|基本题|深入题|扩充题|Problems|Exercises)", re.I)
STOP_RE = re.compile(r"^(附录|参考文献|索引|部分?习题(参考)?答案|习题解答|参考答案|答案|习题提示)")
OPENER_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十百]+\s*章")
# 不带章号的题号：1.  1、  第1题（每章/每大题从 1 开始，或试卷里连续编号）
PLAIN_RE = re.compile(r"^\s*(?:第\s*)?([0-9lIO|]{1,3})\s*(?:[.．、]|题)(?![0-9])")
# 试卷大题标题：一、选择题（每小题3分…）
EXAM_SEC_RE = re.compile(r"^\s*([一二三四五六七八九十]+)\s*[、.．]\s*(\S.*)$")
_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

PAD = 10                 # 切片上下留白 (px)
INK_THRESH = 170         # 灰度小于该值视为墨迹
MAX_SKIP = 2             # 允许跳过的题号个数


# ---------------------------------------------------------------- OCR 并行工作进程
_OCR = None


def _ocr_init():
    global _OCR
    from rapidocr_onnxruntime import RapidOCR
    _OCR = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)


def _ocr_job(job: Tuple[str, int, int, str, str]) -> Tuple[int, Dict[str, Any]]:
    """两种模式，结果缓存在 cache：
    scan : 只做文字行检测(~3s)，并整行识别“短行/大字号行”（标题、章首、页码都在其中），其余行只留框；
    deep : 在 scan 结果上，只识别其余各行的最左侧一小段（题号只需前几个字）。
    整页逐行全文识别在普通 CPU 上要 50~80 秒/页，不可接受。"""
    pdf, pno, dpi, cache, mode = job
    cp = Path(cache)
    data: Optional[Dict[str, Any]] = None
    if cp.exists():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            if isinstance(data, list):                 # 旧格式缓存 = 已深度识别
                data = {"deep": True, "lines": data}
        except Exception:
            data = None
    if data and (mode == "scan" or data.get("deep")):
        return pno, data
    doc = pymupdf.open(pdf)
    pix = doc[pno].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    H, W = gray.shape
    img3 = np.stack([gray] * 3, -1)

    def rec(x0, y0, x1, y1):
        crop = img3[max(0, int(y0) - 2): int(y1) + 2, max(0, int(x0)): max(int(x0) + 8, int(x1))]
        try:
            rr = _OCR(crop, use_det=False, use_cls=False, use_rec=True)
            r = rr[0] if isinstance(rr, tuple) else rr
            if r:
                return r[0][0], float(r[0][1])
        except Exception:
            pass
        return "", 0.0

    if data is None:
        det = _OCR(img3, use_det=True, use_cls=False, use_rec=False)
        boxes = (det[0] if isinstance(det, tuple) else det) or []
        rects = []
        for bx in boxes:
            xs = [q[0] for q in bx]
            ys = [q[1] for q in bx]
            rects.append((float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))))
        med_h = float(np.median([r[3] - r[1] for r in rects])) if rects else 30.0
        lines = []
        for x0, y0, x1, y1 in rects:
            ln = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": "", "score": 0.0, "partial": True, "done": False}
            if (x1 - x0) <= 0.35 * W or (y1 - y0) >= 1.2 * med_h:
                ln["text"], ln["score"] = rec(x0, y0, x1, y1)
                ln["partial"], ln["done"] = False, True
            lines.append(ln)
        lines.sort(key=lambda l: (l["y0"], l["x0"]))
        data = {"deep": False, "lines": lines, "W": W, "H": H, "med_h": med_h}
    if mode == "deep":
        for ln in data["lines"]:
            if not ln["done"]:
                ln["text"], ln["score"] = rec(ln["x0"], ln["y0"], min(ln["x1"], ln["x0"] + 0.22 * W), ln["y1"])
                ln["done"] = True
        data["deep"] = True
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return pno, data


def _is_prose(t) -> bool:
    """扫描件长行只识别了行首几个字，所以有 2 个汉字即算正文；坐标轴刻度、曲线标注一般没有汉字。
    分数线、负号常被 OCR 认成“一”“丨”，不算汉字"""
    han = [ch for ch in re.findall(r"[一-鿿]", t.get("text") or "") if ch not in "一丨丁二十"]
    return len(han) >= 2 and not CAPTION_RE.match((t.get("text") or "").strip())


def _merge_spaced(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """字距很宽的标题“习　题”会被检测成“习”“题”两个框：同一行上相邻的单个汉字框按从左到右合并成一行"""
    short = [i for i, l in enumerate(lines) if not l.get("partial", False)
             and re.fullmatch(r"[一-鿿]{1,2}", (l.get("text") or "").strip())]
    if len(short) < 2:
        return lines
    used, merged = set(), []
    for i in short:
        if i in used:
            continue
        li = lines[i]
        h = li["y1"] - li["y0"]
        row = sorted([j for j in short if j not in used and abs((lines[j]["y0"] + lines[j]["y1"]) - (li["y0"] + li["y1"])) / 2 < 0.5 * h],
                     key=lambda j: lines[j]["x0"])
        grp = [row[0]]
        for j in row[1:]:
            if lines[j]["x0"] - lines[grp[-1]]["x1"] < 3 * h:
                grp.append(j)
            else:
                break
        if len(grp) < 2 or i not in grp:
            continue
        used.update(grp)
        g = [lines[j] for j in grp]
        merged.append(dict(g[0], x0=min(x["x0"] for x in g), x1=max(x["x1"] for x in g), y0=min(x["y0"] for x in g),
                           y1=max(x["y1"] for x in g), text="".join((x.get("text") or "").strip() for x in g)))
    out = [l for k, l in enumerate(lines) if k not in used] + merged
    out.sort(key=lambda l: (l["y0"], l["x0"]))
    return out


def _finder_runs(line: np.ndarray, lo: float, hi: float) -> bool:
    """一行像素里有没有“黑白黑白黑 = 1:1:3:1:1”的定位块剖面，总宽在 [lo, hi]"""
    if not line.any():
        return False
    ch = np.flatnonzero(np.diff(line.astype(np.int8))) + 1
    edges = np.concatenate(([0], ch, [len(line)]))
    runs = np.diff(edges)
    vals = line[edges[:-1]]
    for i in range(len(runs) - 4):
        if not vals[i]:
            continue
        r = runs[i:i + 5].astype(float)
        tot = r.sum()
        if not (lo <= tot <= hi):
            continue
        u = tot / 7.0
        if all(abs(x - e * u) <= 0.6 * u + 1 for x, e in zip(r, (1, 1, 3, 1, 1))):
            return True
    return False


def _has_finders(b: np.ndarray) -> bool:
    """二维码三个角（左上、右上、左下）里至少两个有定位块：横向、纵向都能扫出 1:1:3:1:1"""
    h, w = b.shape
    s = int(0.38 * min(h, w))
    lo, hi = 0.12 * min(h, w), 0.4 * min(h, w)
    hits = 0
    for cy, cx in ((0, 0), (0, w - s), (h - s, 0)):
        win = b[cy:cy + s, cx:cx + s]
        if any(_finder_runs(win[r], lo, hi) for r in range(0, s, 2)) and            any(_finder_runs(win[:, c], lo, hi) for c in range(0, s, 2)):
            hits += 1
    return hits >= 2


def parse_anchor_candidates(text: str) -> List[Tuple[int, int]]:
    """返回所有可能的 (章, 序号)。题号可能和正文粘连(如 '2.692.5节曾...')，
    所以序号取数字串的所有 1~3 位前缀，由状态机按“期望序号”裁决。"""
    m = ANCHOR_RE.match(text)
    if not m:
        return []
    try:
        a = int(m.group(1).translate(_DIGIT_FIX))
    except ValueError:
        return []
    digits = m.group(2).translate(_DIGIT_FIX)
    if not digits.isdigit():
        return []
    return [(a, int(digits[:n])) for n in range(min(3, len(digits)), 0, -1)]


def parse_anchor(text: str) -> Optional[Tuple[int, int]]:
    c = parse_anchor_candidates(text)
    return c[0] if c else None


class OcrBookCropper:
    def __init__(self, pdf_path: str, out_root: Optional[str] = None, dpi: int = 150,
                 workers: Optional[int] = None, page_range: Optional[Tuple[int, int]] = None,
                 log=print, exam: bool = False, only_pages: Optional[set] = None,
                 expected_ids: Optional[set] = None):
        self.pdf_path = Path(pdf_path)
        self.out_root = Path(out_root) if out_root else self.pdf_path.parent
        self.dpi = dpi
        self.scale = dpi / 72.0
        self.doc = pymupdf.open(str(self.pdf_path))
        self.total_pages = len(self.doc)
        # page_range 为 1 基、闭区间
        lo, hi = page_range if page_range else (1, self.total_pages)
        self.pages = list(range(max(1, lo) - 1, min(self.total_pages, hi)))
        self.workers = workers or max(1, min(12, (os.cpu_count() or 4) // 2))
        self.log = log
        # 只扫这些页（1 基页码集合）；其余页当空白页，不做 OCR（导入的 md 里“习题页”给出的范围）
        self.only = {int(p) - 1 for p in only_pages} if only_pages else None
        # 导入文件里的全部题号 {(章, 序号)}：和习题页范围一起给出时进入“信任模式”——
        # 范围内每页都当习题页逐页找题号，只认清单里的题号，不靠“习题”标题、不被“答案/小结”字样截断
        self.expected = {(int(a), int(b)) for a, b in expected_ids} if expected_ids else None
        self.trusted = bool(self.only and self.expected and not exam)
        self.lines: Dict[int, List[Dict[str, Any]]] = {}
        self.deep_done: set = set()
        self.layout: Optional[Dict[str, Any]] = None
        self.exam = exam                      # 扫描版试卷：按“一、选择题”等大题分组，题号为 1. 2. …
        self.section_names: Dict[int, str] = {}
        self.page_chapter: Dict[int, int] = {}
        self.margin = 36          # 正文墨迹外的左右余量（px），裁判发现截断会逐步放宽
        self.text_layer = False
        self._geom: Dict[int, Dict[str, Any]] = {}
        self._term: Dict[int, List[float]] = {}
        self._has_headers: Optional[bool] = None
        self._sim_ys: Dict[int, set] = {}
        self._gray: Dict[int, np.ndarray] = {}
        self._qr: Dict[int, List[Tuple[int, int, int, int]]] = {}

    # ------------------------------------------------------------ 1. 取得每页文字行
    def has_text_layer(self) -> bool:
        sample = [p for p in self.pages if p % 7 == 0][:12] or self.pages[:12]
        n = sum(len(self.doc[p].get_text().strip()) for p in sample)
        return n > 80 * max(1, len(sample)) * 0.5

    def _lines_from_text_layer(self, pno: int) -> List[Dict[str, Any]]:
        s = self.scale
        out = []
        for blk in self.doc[pno].get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                txt = "".join(sp["text"] for sp in ln["spans"]).strip()
                if not txt:
                    continue
                x0, y0, x1, y1 = ln["bbox"]
                out.append({"x0": x0 * s, "y0": y0 * s, "x1": x1 * s, "y1": y1 * s, "text": txt, "score": 1.0})
        out.sort(key=lambda l: (l["y0"], l["x0"]))
        return out

    def _cache_path(self, pno: int) -> str:
        return str(self.out_root / "_cache" / f"ocr_dpi{self.dpi}" / f"p{pno:04d}.json")

    def _job(self, pno: int, mode: str):
        return (str(self.pdf_path), pno, self.dpi, self._cache_path(pno), mode)

    def scan_all(self):
        """阶段 A：全书扫描（文字层则直接读取，扫描版则 det + 标题级短行识别）"""
        if self.has_text_layer():
            self.log(f"[*] 检测到文字层，直接读取文字坐标（{len(self.pages)} 页），不做 OCR")
            self.text_layer = True
            for p in self.pages:
                self.lines[p] = self._lines_from_text_layer(p)
                self.deep_done.add(p)
            return
        self.text_layer = False
        todo_pages = []
        if self.only is not None:
            for p in self.pages:
                if p not in self.only:
                    self.lines[p] = []
                    self.deep_done.add(p)
            self.log(f"[*] 按导入文件给出的习题页范围，只扫描 {len([p for p in self.pages if p in self.only])}/{len(self.pages)} 页")
        for p in self.pages:
            if p in self.deep_done and not self.lines.get(p) and self.only is not None and p not in self.only:
                continue
            cp = Path(self._cache_path(p))
            data = None
            if cp.exists():
                try:
                    data = json.loads(cp.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        data = {"deep": True, "lines": data}
                except Exception:
                    data = None
            if data is None:
                todo_pages.append(p)
            else:
                self.lines[p] = data["lines"]
                if data.get("deep"):
                    self.deep_done.add(p)
        self.log(f"[*] 扫描版 PDF：{len(self.pages)} 页；阶段A 扫描标题，{len(todo_pages)} 页需要处理（{self.workers} 进程，缓存可断点续跑）")
        if todo_pages:
            from concurrent.futures.process import BrokenProcessPool
            total, done = len(todo_pages), 0
            pending = list(todo_pages)
            # OCR 进程偶尔会被系统杀掉（内存紧张等）：自动减少进程数重试；单进程仍崩的那一页跳过（记为空页）
            plan = [self.workers, max(2, self.workers // 3), 1]
            attempt = 0
            while pending:
                w = plan[min(attempt, len(plan) - 1)]
                try:
                    with ProcessPoolExecutor(max_workers=w, initializer=_ocr_init) as ex:
                        for pno, data in ex.map(_ocr_job, [self._job(p, "scan") for p in pending], chunksize=1):
                            self.lines[pno] = data["lines"]
                            if data.get("deep"):
                                self.deep_done.add(pno)
                            done += 1
                            if done % 5 == 0 or done == total:
                                self.log(f"    扫描进度 {done}/{total}")
                    pending = []
                except BrokenProcessPool:
                    pending = [p for p in pending if p not in self.lines and not Path(self._cache_path(p)).exists()]
                    attempt += 1
                    self.log(f"    [!] OCR 进程异常退出，剩 {len(pending)} 页，改用 {plan[min(attempt, len(plan) - 1)]} 个进程重试")
                    if attempt > len(plan) and pending:   # 单进程也崩：这一页放弃
                        bad = pending.pop(0)
                        self.lines[bad] = []
                        self.log(f"    [!] 第 {bad + 1} 页 OCR 反复崩溃，已跳过")
                    if not pending:
                        break
            # 崩溃前已写入缓存、但没来得及回传的页：读回
            for p in self.pages:
                if p not in self.lines:
                    cp = Path(self._cache_path(p))
                    if cp.exists():
                        try:
                            d = json.loads(cp.read_text(encoding="utf-8"))
                            self.lines[p] = d["lines"] if isinstance(d, dict) else d
                            if isinstance(d, dict) and d.get("deep"):
                                self.deep_done.add(p)
                        except Exception:
                            pass

    # ------------------------------------------------------------ 2. 页面几何
    def gray(self, pno: int) -> np.ndarray:
        if pno not in self._gray:
            pix = self.doc[pno].get_pixmap(dpi=self.dpi, colorspace=pymupdf.csGRAY)
            if len(self._gray) >= 6:
                self._gray.pop(next(iter(self._gray)))
            self._gray[pno] = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        return self._gray[pno]

    def geom(self, pno: int) -> Dict[str, Any]:
        """页眉下沿、页脚上沿、正文左右边界、行高中位数"""
        if pno in self._geom:
            return self._geom[pno]
        g = self.gray(pno)
        H, W = g.shape
        lines = self.lines.get(pno, [])
        # 页眉：顶部 14% 内找长横线，否则取顶部 9% 内文字行下沿
        header_cut = 0
        dark = (g[: int(H * 0.14), int(W * 0.1): int(W * 0.9)] < INK_THRESH).mean(axis=1)
        rule_rows = np.where(dark > 0.55)[0]
        if len(rule_rows):
            header_cut = int(rule_rows.max()) + 3
        else:
            # 长得像题号的行（如页面第一道题“2.19 试问…”）不是页眉文字，否则整行会被当页眉丢掉
            top_lines = [l for l in lines if l["y1"] < H * 0.09 and not parse_anchor_candidates((l.get("text") or "").strip())]
            # 整本书没有页眉时（页首那行是上一页题目的续行），不能把它当页眉切掉
            if top_lines and self._book_has_headers():
                header_cut = int(max(l["y1"] for l in top_lines)) + 4
        # “习题6-21 …”这种行不会是页眉：顶部的横线若在它下面，是插图坐标轴之类，不是页眉线
        pref_top = [l["y0"] for l in lines if l["y0"] < header_cut and PREFIX_RE.match(l.get("text") or "")]
        if pref_top:
            header_cut = max(0, int(min(pref_top)) - 4)
        # 页脚：底部 6% 内的短文字（页码）
        # 只认长得像页码的行（纯数字 / “- 12 -” / 罗马数字）；公式下限“m=-∞”这类短行也会落在页面底部
        foot_lines = [l for l in lines if l["y0"] > H * 0.94
                      and re.fullmatch(r"[\s\-—–·]*(\d{1,4}|[ivxlcIVXLC]{1,6})[\s\-—–·]*", l["text"] or "")]
        # 有的书页码印成“// 34 //”“/ 120 /”，位置也更高（约 91% 处）
        foot_lines += [l for l in lines if l["y0"] > H * 0.85 and (l["x1"] - l["x0"]) < 0.25 * W
                       and "/" in (l["text"] or "")
                       and re.fullmatch(r"[\s/|\\Il1\-—–·]*\d{1,4}[\s/|\\Il1\-—–·]*", l["text"] or "")]
        footer_cut = int(min(l["y0"] for l in foot_lines)) - 3 if foot_lines else int(H * 0.97)
        # 脚注：页面下部左侧的一条短横线（约 1/5~2/5 版心宽、右侧空白）是脚注分隔线，其下内容不属于题目
        # 只在页面最下 28% 找（电路图里的水平导线上下也是空白，常被误认成分隔线，把下面的题整段丢掉）
        lo = int(H * 0.72)
        band = g[lo:footer_cut] < INK_THRESH
        if band.size:
            left = band[:, int(W * 0.04): int(W * 0.5)].mean(axis=1)
            right = band[:, int(W * 0.55): int(W * 0.96)].mean(axis=1)
            for k in np.where((left > 0.25) & (right < 0.01))[0]:
                y_rule = lo + int(k)
                row = band[k, int(W * 0.04): int(W * 0.5)]
                # 必须是一条连续细实线：最长连续墨迹段足够长（文字行只有零散短段）
                run = best = 0
                for v in row:
                    run = run + 1 if v else 0
                    best = max(best, run)
                if best < 0.12 * W:
                    continue
                above = g[max(0, y_rule - 9): y_rule - 2] < INK_THRESH
                below = g[y_rule + 3: y_rule + 8, int(W * 0.04): int(W * 0.5)] < INK_THRESH
                # 上下都是空白，才是脚注分隔线（而不是分数线、下划线、表格线）
                # 线下面有题号行（“习题6-2 …”）就不是脚注
                if any(l["y0"] > y_rule and parse_anchor_candidates((l.get("text") or "").strip()) for l in lines):
                    continue
                if above.size and above.mean() < 0.01 and below.size and below.mean() < 0.02:
                    footer_cut = min(footer_cut, y_rule - 4)
                    break
        # 正文左右边界
        body = g[header_cut:footer_cut, int(W * 0.02): int(W * 0.98)] < INK_THRESH
        cols = np.where(body.sum(axis=0) >= 3)[0]
        ink_l = int(W * 0.02) + int(cols.min()) if len(cols) else 0
        ink_r = int(W * 0.02) + int(cols.max()) if len(cols) else W
        xl = max(0, ink_l - 30)
        xr = min(W, ink_r + 30)
        heights = [l["y1"] - l["y0"] for l in lines if l["y0"] > header_cut]
        # 正文栏左右边界：取长行（整行正文）的左端/右端中位数；页边栏（二维码说明等）完全在它外面
        longs = [l for l in lines if l["x1"] - l["x0"] > 0.4 * W and header_cut < l["y0"] < footer_cut]
        col_l = float(np.median([l["x0"] for l in longs])) if len(longs) >= 3 else 0.0
        col_r = float(np.median([l["x1"] for l in longs])) if len(longs) >= 3 else float(W)
        res = {"W": W, "H": H, "header_cut": header_cut, "footer_cut": footer_cut, "col_l": col_l, "col_r": col_r,
               "xl": xl, "xr": xr, "ink_l": ink_l, "ink_r": ink_r,
               "med_h": float(np.median(heights)) if heights else 30.0}
        self._geom[pno] = res
        return res

    def _book_has_headers(self) -> bool:
        """抽样各页：页面最上面一行和下一行之间隔着明显空白 = 有页眉。多数页如此才算这本书有页眉。"""
        if self._has_headers is None:
            votes = []
            for p in [q for q in self.pages if self.lines.get(q)][:: 3][:40]:
                ls = sorted(self.lines[p], key=lambda l: l["y0"])
                H = self.gray(p).shape[0]
                top = [l for l in ls if l["y1"] < H * 0.09]
                if not top:
                    continue
                hb = max(l["y1"] for l in top)
                below = [l["y0"] for l in ls if l["y0"] > hb]
                mh0 = float(np.median([l["y1"] - l["y0"] for l in ls]))
                votes.append(not below or min(below) - hb >= 0.9 * mh0)
            self._has_headers = (sum(votes) >= 0.5 * len(votes)) if len(votes) >= 4 else True
            self.log(f"[*] 页眉判定：{'有' if self._has_headers else '无'}页眉（抽样 {len(votes)} 页）")
        return self._has_headers

    def body_lines(self, pno: int) -> List[Dict[str, Any]]:
        g = self.geom(pno)
        return _merge_spaced([l for l in self.lines.get(pno, []) if l["y1"] > g["header_cut"] and l["y0"] < g["footer_cut"]])

    # ------------------------------------------------------------ 3. 锚点识别
    # ------------------------------------------------------------ 3. 锚点识别
    def _line_kind(self, ln: Dict[str, Any], g: Dict[str, Any]) -> Optional[str]:
        """整行识别过的行才可能是 stop / opener / heading"""
        if ln.get("partial", False) or not ln.get("text"):
            return None
        tc = re.sub(r"\s+", "", ln["text"])
        # 页边栏的二维码说明（“典型例题”“思考题”“本章小结”）不是版心里的标题
        if ln["x1"] < g.get("col_l", 0) - 8 or ln["x0"] > g.get("col_r", g["W"]) + 8:
            return None
        # 答案区标题：短、不含句中标点（“直接计算α，并验证你的答案。”这类题干句子不能算）
        is_title_like = len(tc) <= 16 and not re.search(r"[，。；！？,;!?]", tc)
        if is_title_like and (STOP_RE.match(tc) or (re.search(r"(答案|解答|题解)", tc)
                                                     and not re.search(r"[附带含]答案", tc))):
            return "stop"
        # 章首标题：大字号且不在页面顶部（顶部 12% 的“第N章…”是每页都有的页眉，不能当成新一章开始）
        if (OPENER_RE.match(tc) and (ln["y1"] - ln["y0"]) >= 1.35 * g["med_h"]
                and ln["y0"] > 0.12 * g["H"]):
            return "opener"
        if len(tc) <= 14 and HEADING_RE.search(tc) and not parse_anchor(ln["text"]):
            return "heading"
        # 带节号的习题标题：“4.7 Exercises”“3.9 习题”（节号形如题号，不能因此当成题号）
        if len(tc) <= 24 and re.fullmatch(r"(?:§)?\d{1,2}(?:[.．]\d{1,2})*\s*(?:习题|练习题|思考题|Exercises|Problems)", tc, re.I):
            return "heading"
        return None

    def classify_pages(self) -> Dict[int, str]:
        """阶段A 结果：标出全书的 heading / opener / stop 页（只用标题级整行文字）"""
        flags: Dict[int, str] = {}
        for pno in self.pages:
            g = self.geom(pno)
            right_nums = sum(1 for ln in self.lines.get(pno, [])
                             if not ln.get("partial", False) and re.fullmatch(r"\d{1,3}", ln.get("text", "").strip())
                             and ln["x0"] > 0.8 * g["W"])
            if right_nums >= 5:
                flags[pno] = "toc"
                continue
            for ln in self.body_lines(pno):
                k = self._line_kind(ln, g)
                if k in ("stop", "opener"):
                    # 同页先有“习题”标题、后有“第N章习题答案”（二维码说明）：仍是习题标题页，页内的结束标记由状态机逐行处理
                    if flags.get(pno) != "heading":
                        flags[pno] = k
                elif k == "heading":
                    flags.setdefault(pno, "heading")
        return flags

    def _feed_page(self, pno: int, st: Dict[str, Any], anchors: List[Dict[str, Any]]) -> int:
        """把一页（已深度识别）喂给题号状态机，返回本页新增锚点数"""
        g = self.geom(pno)
        n_before = len(anchors)
        for ln in self.body_lines(pno):
            if self.exam and ln.get("text"):
                ms = EXAM_SEC_RE.match(ln["text"].strip())
                if ms and ln["x0"] < g["W"] * 0.4 and ("题" in ms.group(2) or "分" in ms.group(2)):
                    st["sec"] = _CN.get(ms.group(1)[0], st.get("sec", 0) + 1)
                    self.section_names[st["sec"]] = ln["text"].strip()[:30]
                    st["in_section"], st["heading_page"], st["sec_start"] = True, pno, True
                    continue
            kind = self._line_kind(ln, g)
            if self.trusted and kind in ("stop", "opener"):
                continue          # 信任模式：“答案/小结/章首”字样不结束习题区，一直找到范围末尾
            if kind == "opener":
                mo = re.match(r"^第\s*([0-9]+)\s*章", re.sub(r"\s+", "", ln["text"]))
                if mo and st.get("last_ch") is not None and int(mo.group(1)) == st["last_ch"]:
                    continue      # 当前章的“第N章”字样（页眉/小标题）不结束习题区
            if kind in ("stop", "opener"):
                st["in_section"] = False
                continue
            if kind == "heading":
                st["in_section"] = True
                st["heading_page"] = pno
                continue
            if not st["in_section"] or ln["x0"] > g["W"] * 0.5 or not ln.get("text"):
                continue
            cands = [] if st.get("mode") == "plain" else parse_anchor_candidates(ln["text"].strip())
            # 本习题区的题号带“习题”前缀时，不带前缀的行（“0. 1 μF”“2. 1 kΩ”这类数值）不算题号
            pref = bool(PREFIX_RE.match(ln["text"]))
            if self.trusted:
                # 信任模式：只收集清单里的候选题号，整段范围看完后再挑最长的递增序列（见 _trusted_chain）
                ok = [c for c in cands if c in self.expected]
                if ok:
                    st.setdefault("cand_lines", []).append((pno, ln, ok))
                continue
            if cands and st.get("pref") and not pref:
                continue
            if not cands:
                if st.get("mode") != "prefixed" and self._feed_plain(pno, ln, st, anchors):
                    continue
                continue
            last_ch, expected = st["last_ch"], st["expected"]
            chosen, gap = None, []
            for (a, b) in cands:
                if last_ch is not None and a == last_ch and b == expected:
                    chosen = (a, b)
                    break
            if chosen is None:
                for (a, b) in cands:
                    if last_ch is not None and a == last_ch and expected < b <= expected + MAX_SKIP:
                        chosen, gap = (a, b), list(range(expected, b))
                        break
            if chosen is None:
                for (a, b) in cands:
                    if b == 1 and a >= 1 and pno - st["heading_page"] <= 1:
                        chosen = (a, b)
                        break
            if chosen is None:
                continue
            a, b = chosen
            st["last_ch"], st["expected"], st["mode"] = a, b + 1, "prefixed"
            if st.get("pref") is None:
                st["pref"] = pref
            anchors.append({"pid": f"{a}.{b}", "ch": a, "k": b, "pno": pno, "y0": ln["y0"],
                            "x0": ln["x0"], "score": ln["score"], "gap_before": gap})
        return len(anchors) - n_before

    def _feed_plain(self, pno: int, ln: Dict[str, Any], st: Dict[str, Any], anchors: List[Dict[str, Any]]) -> bool:
        """不带章号的题号（1. / 1、 / 第1题）。章号取：试卷=当前大题序号；教材=页眉/章首的“第N章”，没有则按习题区顺序。"""
        m = PLAIN_RE.match(ln["text"].strip())
        if not m:
            return False
        try:
            b = int(m.group(1).translate(_DIGIT_FIX))
        except ValueError:
            return False
        if self.exam:
            ch = st.get("sec") or 1
        else:
            ch = self.page_chapter.get(pno) or st.get("run_idx", 1)
        exp = st["plain_expected"].get(ch, 1)
        gap: List[int] = []
        if b == exp:
            pass
        elif exp < b <= exp + MAX_SKIP:
            gap = list(range(exp, b))
        elif b == 1 and (pno - st["heading_page"] <= 1 or st.get("sec_start")):
            pass
        elif self.exam and st.get("sec_start") and b == st.get("plain_last", 0) + 1:
            pass    # 试卷题号跨大题连续编号（如一、1-5 二、6-10）
        else:
            return False
        st["plain_expected"][ch] = b + 1
        st["plain_last"], st["mode"], st["sec_start"] = b, "plain", False
        anchors.append({"pid": f"{ch}.{b}", "ch": ch, "k": b, "pno": pno, "y0": ln["y0"],
                        "x0": ln["x0"], "score": ln["score"], "gap_before": gap})
        return True

    @staticmethod
    def resolve_groups(anchors: List[Dict[str, Any]], log=print) -> List[Dict[str, Any]]:
        """同一章若出现多组从 1 开始的序列（目录、答案区、正文节号造成的误识别），保留题数最多的一组。"""
        seg_of: Dict[int, int] = {}
        groups: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        for a in anchors:
            if a["k"] == 1 or a["ch"] not in seg_of:
                seg_of[a["ch"]] = seg_of.get(a["ch"], -1) + 1
            groups.setdefault((a["ch"], seg_of[a["ch"]]), []).append(a)
        keep = []
        for ch in sorted({c for c, _ in groups}):
            cands = [(len(g), seg, g) for (c, seg), g in groups.items() if c == ch]
            cands.sort(key=lambda t: (t[0], t[1]))
            best = cands[-1][2]
            # 书末“习题答案”区里的题号序列和习题题数相近（甚至一样多）：题数接近时取靠前的一组（习题在前，答案在后）
            near = [g for n, seg, g in cands if n >= 0.6 * len(best) and n >= 3]
            if near:
                best = min(near, key=lambda g: (g[0]["pno"], g[0]["y0"]))
            dropped = [(len(g), g[0]["pno"] + 1) for n, seg, g in cands if g is not best]
            if dropped:
                log(f"    第{ch}章: 丢弃 {len(dropped)} 组疑似误识别序列 (题数, 起始页) {dropped}")
            keep.extend(best)
        # 孤零零的一个“章”、章号远大于其它章（如节标题“§9.1”被读成“89.1”）：是误识别，丢掉
        by_ch: Dict[int, int] = {}
        for a in keep:
            by_ch[a["ch"]] = by_ch.get(a["ch"], 0) + 1
        solid = [c for c, n in by_ch.items() if n >= 3]
        if solid:
            top = max(solid)
            lone = [c for c, n in by_ch.items() if n <= 2 and c > top + 3]
            if lone:
                log(f"    丢弃疑似误识别的孤立章号 {lone}（章号远大于正文最大章 {top}，常见于节标题“§9.1”被读成“89.1”）")
                keep = [a for a in keep if a["ch"] not in lone]
        keep.sort(key=lambda a: (a["pno"], a["y0"]))
        return keep

    def chapter_titles(self) -> Dict[str, str]:
        """从章首/页眉文字（如“第2章 光纤和光缆”）取各章标题，取出现次数最多者"""
        from collections import Counter
        cnt: Dict[int, Counter] = {}
        cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        for pno in self.pages:
            H = self.geom(pno)["H"]
            for ln in self.lines.get(pno, []):
                if ln.get("partial", False) or not ln.get("text") or ln["y0"] > H * 0.2:
                    continue
                m = re.match(r"^第\s*([0-9一二三四五六七八九十]+)\s*章\s*(.{2,24})$", ln["text"].strip())
                if not m:
                    continue
                num = int(m.group(1)) if m.group(1).isdigit() else cn.get(m.group(1), 0)
                title = re.sub(r"\s+", "", m.group(2))
                if num and not re.search(r"\d{2,}$", title):
                    cnt.setdefault(num, Counter())[title] += 1
        return {str(n): c.most_common(1)[0][0] for n, c in cnt.items()}

    def _page_chapters(self) -> Dict[int, int]:
        """每页所属章号：取页眉/章首里的“第N章”，向后填充（供不带章号的题号使用）。"""
        out: Dict[int, int] = {}
        cur = None
        for pno in self.pages:
            H = self.geom(pno)["H"]
            for ln in self.lines.get(pno, []):
                t = (ln.get("text") or "").strip()
                m = re.match(r"^第\s*([0-9一二三四五六七八九十]+)\s*章", t)
                if m and (ln["y0"] < H * 0.2 or not ln.get("partial", False)):
                    v = m.group(1)
                    cur = int(v) if v.isdigit() else _CN.get(v[-1], cur)
                    break
            if cur:
                out[pno] = cur
        return out

    def _trusted_chain(self, cand_lines, taken: set) -> List[Dict[str, Any]]:
        """信任模式：一段习题页里所有像题号的行（只含清单里的题号）中，选出题号严格递增的最长序列。
        正文里的节号“7.1 7.2”连不成长串；长度相同时取位置靠后的（题目在正文之后）。"""
        items = [(i, c) for i, (_, _, cs) in enumerate(cand_lines) for c in cs if c not in taken]
        n = len(items)
        if not n:
            return []
        L, prev = [1] * n, [-1] * n
        for x in range(n):
            ix, cx = items[x]
            for y in range(x):
                iy, cy = items[y]
                if iy < ix and cy < cx and (L[y] + 1 > L[x] or (L[y] + 1 == L[x] and y > prev[x])):
                    L[x], prev[x] = L[y] + 1, y
        end = max(range(n), key=lambda x: (L[x], x))
        chain = []
        while end >= 0:
            chain.append(items[end])
            end = prev[end]
        chain.reverse()
        out, last = [], {}
        for i, (a, b) in chain:
            pno, ln, _ = cand_lines[i]
            lo = last.get(a, 0)
            gap = [k for k in range(lo + 1, b) if (a, k) in self.expected and (a, k) not in taken]
            last[a] = b
            out.append({"pid": f"{a}.{b}", "ch": a, "k": b, "pno": pno, "y0": ln["y0"],
                        "x0": ln["x0"], "score": ln["score"], "gap_before": gap})
        return out

    PREFETCH = 3
    EMPTY_STOP = 2     # 连续这么多页没有新题号，就认为习题区结束

    def find_anchors(self) -> Tuple[List[Dict[str, Any]], Dict[int, str]]:
        """阶段B：只对“习题标题页”起的连续页面做深度识别，边识别边走状态机，直到连续无题号页。"""
        page_flag = self.classify_pages()
        heads = [p for p in self.pages if page_flag.get(p) == "heading"]
        self.log(f"[*] 阶段A 完成：发现 {len(heads)} 个习题标题页 {[h + 1 for h in heads]}")
        # 给了习题页范围（导入文件 / 目录定位）：某段范围里没认出“习题”标题（字距太宽、版式特殊），
        # 就把这段的第一页当标题页，不让整章因为标题没认出来而丢掉
        forced: set = set()
        run_end: Dict[int, int] = {}
        if self.trusted:
            runs, prev = [], None
            for q in sorted(self.only):
                if prev is None or q != prev + 1:
                    runs.append([q, q])
                else:
                    runs[-1][1] = q
                prev = q
            # 每段从段内第一个“习题”标题页开始（标题前面是正文，正文里的节号“1.2”也在题号清单里，不能收）；
            # 段内认不出标题才从段首页开始
            new_heads = []
            for lo, hi in runs:
                hs = [h for h in heads if lo <= h <= hi]
                st0 = hs[0] if hs else lo
                if st0 not in self.pages:
                    continue
                new_heads.append(st0)
                run_end[st0] = hi
                if not hs:
                    forced.add(st0)
            heads = new_heads
            self.log(f"[*] 信任导入的习题页范围：{len(runs)} 段，逐页按题号清单（{len(self.expected)} 题）找题")
        elif self.only and not self.exam:
            runs, prev = [], None
            for q in sorted(self.only):
                if prev is None or q != prev + 1:
                    runs.append([q, q])
                else:
                    runs[-1][1] = q
                prev = q
            for lo, hi in runs:
                if not any(lo <= h <= hi for h in heads) and lo in self.pages:
                    forced.add(lo)
            if forced:
                self.log(f"    [!] 这些习题页范围里没认出“习题”标题，从范围首页开始找题号：{sorted(f + 1 for f in forced)}")
                heads = sorted(set(heads) | forced)
        anchors: List[Dict[str, Any]] = []
        st = {"in_section": self.exam, "heading_page": self.pages[0] if (self.exam and self.pages) else -99,
              "last_ch": None, "expected": 1, "mode": None, "plain_expected": {}, "run_idx": 0}
        self.page_chapter = self._page_chapters()
        if self.exam and self.pages:
            heads = [self.pages[0]]
        processed: set = set()
        ex = None if self.text_layer else ProcessPoolExecutor(max_workers=self.workers, initializer=_ocr_init)
        futs: Dict[int, Any] = {}
        last_page = self.pages[-1] if self.pages else -1

        def get_deep(p: int):
            if p in self.deep_done or self.text_layer:
                return
            for q in range(p, min(last_page, p + self.PREFETCH) + 1):
                if q not in futs and q not in self.deep_done:
                    futs[q] = ex.submit(_ocr_job, self._job(q, "deep"))
            _, data = futs.pop(p).result()
            self.lines[p] = data["lines"]
            self.deep_done.add(p)
            self._geom.pop(p, None)

        try:
            for h in heads:
                if h in processed:
                    continue
                st["mode"], st["run_idx"], st["pref"] = None, st["run_idx"] + 1, None   # 每个习题区独立判定题号格式
                if h in forced:
                    st["in_section"], st["heading_page"] = True, h
                p, empty = h, 0
                while p <= last_page and (empty < self.EMPTY_STOP or self.exam or h in run_end):
                    if h in run_end and p > run_end[h]:
                        break
                    if p != h and page_flag.get(p) == "toc" and h not in run_end:
                        break
                    get_deep(p)
                    found = self._feed_page(p, st, anchors)
                    if p != h and page_flag.get(p) in ("opener", "stop") and h not in run_end:
                        # 结束标记之前的题照收（如页底二维码“第1章习题答案”上方的最后几题），这一页之后不再往下走
                        processed.add(p)
                        break
                    processed.add(p)
                    empty = 0 if found else empty + 1
                    p += 1
                    if self.text_layer is False and (p - h) % 5 == 0:
                        self.log(f"    深度识别 第{p}页，已识别题号 {len(anchors)} 个")
                if self.trusted:
                    got = self._trusted_chain(st.pop("cand_lines", []), {(x["ch"], x["k"]) for x in anchors})
                    anchors.extend(got)
                st["in_section"] = self.exam
        finally:
            if ex:
                ex.shutdown(wait=False, cancel_futures=True)
        anchors = self.resolve_groups(anchors, self.log)
        return self._recover_gaps(anchors), page_flag

    def _recover_gaps(self, anchors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """漏号补识别：在前后两题之间、题号所在的那一列，用 2 倍分辨率重新 OCR，专门找缺失的题号。
        OCR 偶尔把行首题号和后面的公式粘在一起识别错（如“2. ∮…”读成“P2|=1”），这里补回来。"""
        todo = [i for i, a in enumerate(anchors) if a.get("gap_before") and i > 0]
        if not todo:
            return anchors
        try:
            from rapidocr_onnxruntime import RapidOCR
            eng = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        except Exception:
            return anchors
        added = []
        for i in todo:
            prev, cur = anchors[i - 1], anchors[i]
            if prev["ch"] != cur["ch"]:
                continue
            x0 = max(0, int(min(prev["x0"], cur["x0"])) - 15)
            regions = ([(cur["pno"], prev["y0"] + 8, cur["y0"] - 4)] if prev["pno"] == cur["pno"] else
                       [(prev["pno"], prev["y0"] + 8, self.geom(prev["pno"])["footer_cut"]),
                        (cur["pno"], self.geom(cur["pno"])["header_cut"], cur["y0"] - 4)])
            missing = set(cur["gap_before"])
            for pno, ya, yb in regions:
                if not missing or yb - ya < 10:
                    continue
                W = self.geom(pno)["W"]
                zoom = 2.0
                clip = pymupdf.Rect(x0 / self.scale, ya / self.scale, (x0 + 0.2 * W) / self.scale, yb / self.scale)
                pix = self.doc[pno].get_pixmap(dpi=int(self.dpi * zoom), clip=clip, colorspace=pymupdf.csGRAY)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
                try:
                    res, _ = eng(np.stack([img] * 3, -1))
                except Exception:
                    res = None
                for bbox, text, score in (res or []):
                    t = text.strip()
                    k = None
                    pa = parse_anchor_candidates(t)
                    for (a, b) in pa:
                        if a == cur["ch"] and b in missing:
                            k = b
                            break
                    if k is None:
                        mp = PLAIN_RE.match(t)
                        if mp and mp.group(1).translate(_DIGIT_FIX).isdigit() and int(mp.group(1).translate(_DIGIT_FIX)) in missing:
                            k = int(mp.group(1).translate(_DIGIT_FIX))
                    if k is None:
                        continue
                    y_abs = ya + min(q[1] for q in bbox) / zoom
                    added.append({"pid": f"{cur['ch']}.{k}", "ch": cur["ch"], "k": k, "pno": pno, "y0": y_abs,
                                  "x0": x0 + min(q[0] for q in bbox) / zoom, "score": float(score),
                                  "gap_before": [], "recovered": True})
                    missing.discard(k)
            cur["gap_before"] = sorted(missing)
        if added:
            self.log(f"[*] 漏号补识别：找回 {[a['pid'] for a in added]}")
            anchors = sorted(anchors + added, key=lambda a: (a["pno"], a["y0"]))
        return anchors

    def _ink_trim(self, pno: int, y0: int, y1: int, trim_top: bool) -> Optional[Tuple[int, int]]:
        g = self.gray(pno)
        y0, y1 = max(0, int(y0)), min(g.shape[0], int(y1))
        if y1 - y0 < 8:
            return None
        gm = self.geom(pno)
        rows = (g[y0:y1, gm["xl"]:gm["xr"]] < INK_THRESH).sum(axis=1) >= 3
        idx = np.where(rows)[0]
        if not len(idx):
            return None
        # 页码等页脚：最下面一小块墨迹（矮、窄），和上面的内容隔着一大段空白 -> 不算题目内容，
        # 否则题目下方的整片答题空白会因为它而保留下来（试卷尤其明显）
        for _ in range(2):
            k = len(idx) - 1
            while k > 0 and idx[k] - idx[k - 1] <= max(2, 0.5 * gm["med_h"]):   # 细笔画字形逐行扫描会断开
                k -= 1
            band = idx[k:]
            if k == 0 or band[-1] - band[0] + 1 > 1.5 * gm["med_h"] or band[0] - idx[k - 1] < 3 * gm["med_h"]:
                break
            cols = (g[y0 + band[0]:y0 + band[-1] + 1, gm["xl"]:gm["xr"]] < INK_THRESH).any(axis=0)
            if cols.sum() > 0.12 * (gm["xr"] - gm["xl"]):
                break
            idx = idx[:k]
        top = y0 + (int(idx.min()) - PAD if trim_top else 0)
        bot = y0 + int(idx.max()) + PAD
        return max(0, min(top, y1)), min(g.shape[0], bot)

    def _boundary_above(self, pno: int, y: float) -> int:
        """题与题之间的切分线：从题号所在位置向上，先走出本行墨迹，再找到行间空白带，取空白带中间。
        题号框往往比同行的汉字/公式矮，直接用“题号框 - 固定像素”会切进上一行或本行（顶部被削、上一题尾巴混入）。"""
        g = self.gray(pno)
        gm = self.geom(pno)
        y = int(min(max(y, 0), g.shape[0] - 1))
        limit = max(gm["header_cut"], y - int(3 * gm["med_h"]) - 20)
        # 只看版心左侧 70%：题目文字从左起排；右侧的“得分”框、公式编号等竖向元素会让每一行都“有墨迹”，找不到行间空白
        cr = gm["xl"] + int(0.7 * (gm["xr"] - gm["xl"]))
        ink = (g[limit:y + 1, gm["xl"]:cr] < INK_THRESH).sum(axis=1) >= 2
        k = len(ink) - 1
        while k >= 0 and ink[k]:          # 1) 走出本行（题号所在行）的墨迹
            k -= 1
        gap_bottom = k
        while k >= 0 and not ink[k]:      # 2) 穿过行间空白
            k -= 1
        gap_top = k + 1
        if gap_bottom < 0:                # 到了搜索上限仍在墨迹里：退回旧规则
            return max(gm["header_cut"], y - PAD)
        # 3) 细缝上方紧贴一小块墨迹（如本行求和号的上限 ∞）：它若正对着下面这行的符号、且离下面不比离上面远，
        #    就属于本行，越过它再找真正的行间空白（否则 ∞ 会被切进上一题）
        for _ in range(2):
            gap_h = gap_bottom - gap_top + 1
            b_bot = gap_top - 1
            b_top = b_bot
            while b_top - 1 >= 0 and ink[b_top - 1]:
                b_top -= 1
            if b_bot < 0 or b_top <= 0 or gap_h > 0.35 * gm["med_h"] or (b_bot - b_top + 1) > 0.45 * gm["med_h"]:
                break
            a_bot = b_top - 1                   # 小块上方的空白
            a_top = a_bot
            while a_top - 1 >= 0 and not ink[a_top - 1]:
                a_top -= 1
            if gap_h > (a_bot - a_top + 1) + 2:  # 离上一行更近：是上一行的下标/下限，不动
                break
            frag = (g[limit + b_top:limit + b_bot + 1, gm["xl"]:cr] < INK_THRESH).any(axis=0)
            below = (g[limit + gap_bottom + 1:limit + gap_bottom + 1 + int(0.6 * gm["med_h"]), gm["xl"]:cr] < INK_THRESH).any(axis=0)
            cols = np.flatnonzero(frag)
            if len(cols) == 0 or len(cols) > 0.12 * (cr - gm["xl"]):
                break
            near = np.zeros_like(below)
            for c0 in np.flatnonzero(below):    # 下面这行的符号在横向 ±3px 内
                near[max(0, c0 - 3):c0 + 4] = True
            if near[cols].mean() < 0.6:
                break
            gap_bottom, gap_top = a_bot, a_top
        return max(gm["header_cut"], limit + (gap_top + gap_bottom) // 2)

    def _terminators(self, pno: int) -> List[float]:
        """本页上“题目到此为止”的行：习题区小标题、仿真题区、答案区标题、章首"""
        if pno not in self._term:
            g = self.geom(pno)
            ys = []
            for ln in self.body_lines(pno):
                t = (ln.get("text") or "").strip()
                if not t or ln["x0"] > g["W"] * 0.5:
                    continue
                tc = re.sub(r"\s+", "", t)
                if SIM_RE.match(t):
                    self._sim_ys.setdefault(pno, set()).add(ln["y0"])
                if SIM_RE.match(t) or (not ln.get("partial", False) and (
                        TITLE_RE.match(tc) or self._line_kind(ln, g) in ("stop", "opener"))):
                    ys.append(ln["y0"])
            self._term[pno] = ys
        return self._term[pno]

    def _segments_for(self, i: int, anchors: List[Dict[str, Any]], page_flag: Dict[int, str]):
        """在 _segments_raw 的基础上：遇到小标题 / 仿真题区就截止（本章最后一题不再把后面的仿真题整段带上）"""
        segs, flags = self._segments_raw(i, anchors, page_flag)
        a = anchors[i]
        out = []
        for k, sg in enumerate(segs):
            p, y0, y1 = sg[0], sg[1], sg[2]
            lo = a["y0"] + 5 if (k == 0 and p == a["pno"]) else y0 - 1
            ts = [t for t in self._terminators(p) if lo < t < y1]
            if ts:
                cut = self._boundary_above(p, min(ts))
                if min(ts) in self._sim_ys.get(p, ()):
                    cut = self._skip_dark_bar(p, cut)
                if cut - y0 > 12:
                    out.append((p, y0, cut))
                if len(out) <= 1:
                    flags = [f for f in flags if not f.startswith("stitch")]
                rest = [(p, max(y0, cut), y1)] + list(segs[k + 1:])
                figs = self._own_figures_after(a, rest)
                if figs:
                    flags.append("figure_after_section")
                return (out or [sg]) + figs, flags
            out.append(sg)
        return out, flags

    def _skip_dark_bar(self, p: int, cut: int) -> int:
        """cut 正上方紧挨着一块黑底反白标题条（窄、墨迹很密）时，把截止线移到它上面"""
        g = self.gray(p)
        gm = self.geom(p)
        mh = gm["med_h"]
        lo = max(gm["header_cut"], int(cut - 3 * mh))
        band = g[lo:cut, gm["xl"]:gm["xr"]] < INK_THRESH
        rows = np.flatnonzero(band.sum(axis=1) >= 3)
        if not len(rows):
            return cut
        k = len(rows) - 1                      # 紧挨 cut 的那一块墨迹
        while k > 0 and rows[k] - rows[k - 1] <= 2:
            k -= 1
        r0, r1 = rows[k], rows[-1]
        blk = band[r0:r1 + 1]
        cols = np.flatnonzero(blk.any(axis=0))
        if r1 - r0 < 0.6 * mh or not len(cols):
            return cut
        # 取最长的一段连续墨迹列（标题条右边常跟着几道竖杠“|||”，不能算进来）
        runs, st = [], cols[0]
        for a, b in zip(cols[:-1], cols[1:]):
            if b - a > 4:
                runs.append((st, a))
                st = b
        runs.append((st, cols[-1]))
        c0, c1 = max(runs, key=lambda r: r[1] - r[0])
        row_fill = blk[:, c0:c1 + 1].mean(axis=1)
        # 黑底标题条：多数行几乎整行是墨（正文行即使很密，单行墨迹占比也远低于此）
        if (row_fill > 0.6).mean() >= 0.4 and (c1 - c0) < 0.6 * (gm["xr"] - gm["xl"]):
            return self._boundary_above(p, lo + r0 + 2)
        return cut

    def _own_figures_after(self, a: Dict[str, Any], rest: List[tuple]) -> List[tuple]:
        """截止线之后（仿真题区等）若排着本题的插图（图注是本题题号），把那块插图找回来"""
        out = []
        for sg in rest:
            p, y0, y1 = sg[0], sg[1], sg[2]
            lines = sorted((l for l in self.body_lines(p) if y0 <= l["y0"] and l["y1"] <= y1), key=lambda l: l["y0"])
            for ln in lines:
                m = CAPTION_RE.match((ln.get("text") or "").strip())
                if not m or f"{int(m.group(1))}.{int(m.group(2))}" != a["pid"]:
                    continue
                b_top = y0
                for t in lines:
                    if t["y1"] < ln["y0"] and _is_prose(t):
                        b_top = int(t["y1"]) + 4
                out.append((p, b_top, min(y1, int(ln["y1"]) + 6)))
        return out

    def _segments_raw(self, i: int, anchors: List[Dict[str, Any]], page_flag: Dict[int, str]):
        """返回 [(pno, y_top, y_bottom)] 以及 flags"""
        a = anchors[i]
        nxt = anchors[i + 1] if i + 1 < len(anchors) else None
        p = a["pno"]
        gm = self.geom(p)
        flags: List[str] = []
        segs: List[Tuple[int, int, int]] = []
        top = self._boundary_above(p, a["y0"])
        if nxt and nxt["pno"] == p:
            segs.append((p, top, self._boundary_above(p, nxt["y0"])))
            return segs, flags
        trimmed = self._ink_trim(p, top, gm["footer_cut"], trim_top=False)
        bottom = trimmed[1] if trimmed else gm["footer_cut"]
        segs.append((p, top, bottom))
        reaches_bottom = bottom >= gm["footer_cut"] - 0.08 * gm["H"]
        same_ch = nxt is not None and nxt["ch"] == a["ch"]
        # 跨页续写：当前段触及页底才拼接下一页
        q = p + 1
        if not reaches_bottom or q not in self.lines:
            return segs, flags
        if same_ch:
            while q < nxt["pno"]:
                if page_flag.get(q) in ("opener", "stop", "toc"):
                    return segs, flags
                gq = self.geom(q)
                segs.append((q, gq["header_cut"], gq["footer_cut"]))
                flags.append("stitch_mid_page")
                q += 1
            gq = self.geom(q)
            nb = self._boundary_above(q, nxt["y0"])
            if nb - gq["header_cut"] > 12:
                segs.append((q, gq["header_cut"], nb))
                flags.append("stitched")
        else:
            if page_flag.get(q) in ("opener", "stop", "toc") or any(x["pno"] == q for x in anchors):
                return segs, flags
            gq = self.geom(q)
            segs.append((q, gq["header_cut"], gq["footer_cut"]))
            flags.append("stitched_tail")
        return segs, flags

    # ------------------------------------------------------------ 整本书版式参数
    def build_layout_profile(self, anchors: List[Dict[str, Any]]):
        """抽样习题页，按奇偶页分别用墨迹投影求正文左右边界（装订偏移会让奇偶页不同），整本书统一使用。"""
        pages = sorted({a["pno"] for a in anchors})[:16] or self.pages[:8]
        prof: Dict[str, Any] = {"margin": self.margin, "parity": {}}
        for par in (0, 1):
            ps = [p for p in pages if p % 2 == par] or pages
            ls = sorted(self.geom(p)["ink_l"] for p in ps)
            rs = sorted(self.geom(p)["ink_r"] for p in ps)
            prof["parity"][str(par)] = {"ink_l": ls[len(ls) // 2], "ink_r": rs[len(rs) // 2], "samples": len(ps)}
        self.layout = prof
        return prof

    def crop_x(self, pno: int) -> Tuple[int, int]:
        """左右裁切：取整书参数与本页实际墨迹的并集，再加余量——宁宽勿窄，宽幅插图/表格不会被截。"""
        gm = self.geom(pno)
        m = self.margin
        par = (self.layout or {}).get("parity", {}).get(str(pno % 2))
        l = min(gm["ink_l"], par["ink_l"]) if par else gm["ink_l"]
        r = max(gm["ink_r"], par["ink_r"]) if par else gm["ink_r"]
        return max(0, l - m), min(gm["W"], r + m)

    def judge_slices(self, samples: List[Path]) -> Dict[str, Any]:
        """大模型当裁判：看样例切片有没有被截断或混入别的内容（不让它报坐标）。"""
        try:
            from google.genai import types as gtypes
            from core.solver import init_gemini_client, _call
            client = init_gemini_client({"ai": {}})
            from core import usage
            usage.set_context(self.out_root, None)
        except Exception as e:
            return {"skipped": f"无法连接模型: {e}"}
        prompt = ("这是一张从扫描教材中自动切出的“单道题目”图片。请检查：\n"
                  "1. 左右或上下边缘是否有文字、公式、插图被截断（只露出一部分）；\n"
                  "2. 是否混入了页眉、页码或上一题/下一题的内容。\n"
                  "严格按三行回答：\nTRUNCATED: yes 或 no\nEXTRA: yes 或 no\nDETAIL: 简述（没有问题写 无）")
        results = []
        for sp in samples:
            try:
                part = gtypes.Part.from_bytes(data=Path(sp).read_bytes(), mime_type="image/png")
                ans = _call(client, "gemini-3.8-flash", "你是严谨的排版质检员。", [prompt, part], 0.0, tries=3,
                            stage="crop_judge")
            except Exception as e:
                results.append({"slice": Path(sp).name, "error": str(e)[:200]})
                continue
            trunc = re.search(r"TRUNCATED\s*[:：]\s*(yes|是)", ans, re.I) is not None
            extra = re.search(r"EXTRA\s*[:：]\s*(yes|是)", ans, re.I) is not None
            md = re.search(r"DETAIL\s*[:：]\s*(.*)", ans, re.S)
            results.append({"slice": Path(sp).name, "truncated": trunc, "extra": extra,
                            "detail": (md.group(1).strip()[:200] if md else "")})
        return {"results": results}

    def _apply_masks_to(self, img: Image.Image, segs, masks) -> Image.Image:
        """人工“扣除”的矩形涂白：按各段在拼接图里的纵向偏移换算"""
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        off = 0
        from core.crop_edit import mask_owner
        for idx, ((p, y0, y1, x0, x1), box) in enumerate(zip(segs, self._last_boxes)):
            for m in masks:
                if int(m[0]) == p + 1 and mask_owner(m, self._last_boxes) == idx:
                    d.rectangle([m[3] - box[3], off + m[1] - box[1], m[4] - box[3] - 1, off + m[2] - box[1] - 1], fill=255)
            off += box[2] - box[1]
        return img

    def _qr_boxes(self, p: int) -> List[Tuple[int, int, int, int]]:
        """页面上的二维码（墨迹很密的小方块）及其下方的说明文字（“典型例题”“第1章习题答案”），返回 [(上, 下, 左, 右)]。
        切片里把它们涂白：二维码不是题目内容，却常紧挨在章末最后一题下面或页边。"""
        if p in self._qr:
            return self._qr[p]
        from scipy import ndimage
        g = self.gray(p)
        H, W = g.shape
        c = 6
        h2, w2 = H // c, W // c
        ink = (g[:h2 * c, :w2 * c] < INK_THRESH).reshape(h2, c, w2, c).mean(axis=(1, 3))
        lab, n = ndimage.label(ink > 0.2)
        out = []
        for k, sl in enumerate(ndimage.find_objects(lab), start=1):
            if sl is None:
                continue
            y0, y1, x0, x1 = sl[0].start * c, sl[0].stop * c, sl[1].start * c, sl[1].stop * c
            bh, bw = y1 - y0, x1 - x0
            if not (60 <= bh <= 320 and 60 <= bw <= 320 and 0.7 <= bw / bh <= 1.4):
                continue
            sub = ink[sl[0], sl[1]]
            if (lab[sl[0], sl[1]] == k).mean() < 0.6 or not (0.3 <= sub.mean() <= 0.8):
                continue
            # 必须有二维码的“回”字定位块（三个角里至少两个），密集的波形图、网格阴影图没有
            if not _has_finders(g[y0:y1, x0:x1] < INK_THRESH):
                continue
            # 紧挨在下面的短说明文字一起去掉
            yb = y1
            for ln in self.lines.get(p, []):
                t = re.sub(r"\s+", "", ln.get("text") or "")
                if (0 <= ln["y0"] - yb <= 40 and ln["x1"] > x0 - 40 and ln["x0"] < x1 + 40
                        and 0 < len(t) <= 12 and not parse_anchor_candidates(t)):
                    yb = max(yb, int(ln["y1"]))
            out.append((max(0, y0 - 4), min(H, yb + 4), max(0, x0 - 4), min(W, x1 + 4)))
        self._qr[p] = out
        return out

    def _render_segments(self, segs: List[Tuple[int, int, int]], exact: bool = False) -> Optional[Image.Image]:
        parts = []
        self._last_boxes = []   # 实际切下的区域 [页码(从1起), 上, 下, 左, 右]，供“切题检查”页在原页上画框
        for k, sg in enumerate(segs):
            p, y0, y1 = sg[0], sg[1], sg[2]
            gm = self.geom(p)
            g = self.gray(p)
            if exact:                       # 人工框：按框原样切，不做留白修剪
                y_top, y_bot = y0, y1
            elif k == 0:
                y_top, y_bot = y0, y1
                if len(segs) == 1:
                    # 去掉题目下方的大片空白（试卷作答区、题间留白），保留少量呼吸边距
                    tr = self._ink_trim(p, y0, y1, trim_top=False)
                    if tr:
                        y_bot = min(y1, tr[1])
            else:
                tr = self._ink_trim(p, y0, y1, trim_top=True)
                if not tr:
                    continue
                y_top, y_bot = tr[0], min(y1, tr[1])
            y_top = max(0, y_top)
            y_bot = min(g.shape[0], max(y_top + 1, y_bot))
            cl, cr = (sg[3], sg[4]) if len(sg) > 3 else self.crop_x(p)
            if not exact:
                qrs = [q for q in self._qr_boxes(p) if q[0] < y_bot and q[1] > y_top and q[2] < cr and q[3] > cl]
                if qrs:
                    g = g.copy()
                    for q0, q1, q2, q3 in qrs:
                        g[q0:q1, q2:q3] = 255
                # 段首的“习题”标题行不属于第一题
                if k == 0:
                    for ln in self.body_lines(p):
                        t = re.sub(r"\s+", "", ln.get("text") or "")
                        if (len(t) <= 6 and HEADING_RE.search(t) and y_top - 5 <= ln["y0"] and ln["y1"] < y_bot
                                and gm.get("col_l", 0) - 8 <= ln["x1"] and ln["x0"] <= gm.get("col_r", gm["W"]) + 8):
                            y_top = max(y_top, int(ln["y1"]) + 4)
                # 涂白/去标题后重新收紧上下空白
                rows = np.flatnonzero((g[y_top:y_bot, cl:cr] < INK_THRESH).sum(axis=1) >= 2)
                if len(rows):
                    y_bot = min(y_bot, y_top + int(rows[-1]) + PAD + 1)
                    y_top = max(y_top, y_top + int(rows[0]) - PAD)
            if y_bot - y_top < 2 or cr - cl < 2:
                continue
            parts.append(Image.fromarray(g[y_top:y_bot, cl:cr]))
            self._last_boxes.append([p + 1, int(y_top), int(y_bot), int(cl), int(cr)])
        if not parts:
            return None
        W = max(im.width for im in parts)
        out = Image.new("L", (W, sum(im.height for im in parts)), 255)
        y = 0
        for im in parts:
            out.paste(im, (0, y))
            y += im.height
        return out

    def _text_for(self, segs: List[Tuple[int, int, int]]) -> str:
        """只有文字层 PDF 才能直接取到完整题干；扫描件的 OCR 只识别了零碎行，
        写进题干会冒充完整题目（网页显示残缺、解题阶段也会跳过转写），所以返回空串，交给多模态转写。"""
        if not self.text_layer:
            return ""
        buf = []
        for sg in segs:
            p, y0, y1 = sg[0], sg[1], sg[2]
            for ln in self.body_lines(p):
                yc = (ln["y0"] + ln["y1"]) / 2
                if y0 - 4 <= yc <= y1 + 4 and not ln.get("partial", False):
                    buf.append(ln["text"].strip())
        return "\n".join(buf)

    # ------------------------------------------------------------ 5. 总流程
    def run(self) -> Dict[str, List[Dict[str, Any]]]:
        self.scan_all()
        anchors, page_flag = self.find_anchors()
        self.log(f"[*] 共识别到 {len(anchors)} 个题号锚点")
        if not anchors:
            # 一道题都没识别到：不改动已有的 problems.json / profile，避免把之前的结果清空
            self.log("[!] 未识别到任何题号，保留现有切题结果不变；请检查题号格式或习题标题")
            return {}
        report: Dict[str, Any] = {"pdf": self.pdf_path.name, "dpi": self.dpi, "chapters": {}}
        self.build_layout_profile(anchors)
        edge_log = []
        for attempt in range(3):
            by_ch = self._render_all(anchors, page_flag)
            # 截断用确定性检查：切片左右最外侧几列有墨迹 = 内容贴边被截。全部切片都查，不靠模型判断。
            touching = self._edge_ink_slices(by_ch)
            edge_log.append({"margin": self.margin, "edge_touching": touching[:20]})
            if not touching or attempt == 2:
                break
            self.margin += 40
            self.log(f"[*] {len(touching)} 张切片的内容贴到左右边缘，余量放宽到 {self.margin}px 后重切")
        # 大模型裁判：只作参考记录（实测它对“截断”误报较多，不单独据此改参数）
        samples = self._judge_samples(anchors, by_ch)
        verdict = self.judge_slices(samples) if samples else {"skipped": "无样例"}
        report["layout"] = {"profile": self.layout, "margin": self.margin, "edge_check": edge_log,
                            "llm_judge_advisory": verdict}
        self._finish_report(by_ch, report)
        return by_ch

    def _edge_ink_slices(self, by_ch: Dict[str, List[Dict[str, Any]]]) -> List[str]:
        out = []
        for ch, ps in by_ch.items():
            for p in ps:
                sp = self.out_root / f"Chapter_{int(ch):02d}" / "pages" / p["slice"]
                if not sp.exists():
                    continue
                a = np.asarray(Image.open(sp).convert("L"))
                if a.shape[1] < 20:
                    continue
                # 切片已经是整页全宽时无法再放宽，不计入
                if a.shape[1] >= self.geom(p["page"] - 1)["W"] - 2:
                    continue
                edge = np.concatenate([a[:, :3], a[:, -3:]], axis=1) < INK_THRESH
                if edge.sum() >= 6:
                    out.append(p["problem_id"])
        return out

    def _judge_samples(self, anchors: List[Dict[str, Any]], by_ch: Dict[str, List[Dict[str, Any]]]) -> List[Path]:
        """奇偶页各一张、跨页拼接一张、最高的一张。"""
        items = [(ch, p) for ch, ps in by_ch.items() for p in ps]
        picks = []
        for par in (0, 1):
            for ch, p in items:
                if (p["page"] - 1) % 2 == par:
                    picks.append((ch, p)); break
        for ch, p in items:
            if "stitched" in p["flags"]:
                picks.append((ch, p)); break
        tallest = None
        for ch, p in items:
            sp = self.out_root / f"Chapter_{int(ch):02d}" / "pages" / p["slice"]
            if sp.exists():
                h = Image.open(sp).size[1]
                if tallest is None or h > tallest[0]:
                    tallest = (h, ch, p)
        if tallest:
            picks.append((tallest[1], tallest[2]))
        out, seen = [], set()
        for ch, p in picks:
            sp = self.out_root / f"Chapter_{int(ch):02d}" / "pages" / p["slice"]
            if sp.exists() and sp not in seen:
                seen.add(sp); out.append(sp)
        return out[:4]

    def _reassign_figures(self, anchors: List[Dict[str, Any]], all_segs: List[list], all_flags: List[list]):
        """插图归位：教材常把前一题的插图排在后面（甚至下一页顶部），按切片位置会落进下一题。
        用图注（单独成行的“图 P3.45”）判断插图属于哪道题，补到所属题切片的末尾：
        - 图块里只有插图：从当前题里挖掉（移走）；
        - 图块里还有别题的图注（两题插图并排）：整块复制，当前题不动；
        - 图块旁边排着当前题的文字（图文左右混排）：只横向截取插图那一侧复制过去，当前题不动；
        - 文字和插图分不开：不处理，记 figure_unresolved。
        段可以是 (页, 上, 下) 或 (页, 上, 下, 左, 右)。"""
        pid_idx = {a["pid"]: i for i, a in enumerate(anchors)}

        def owns(j: int, p: int, yc: float) -> bool:
            return any(sg[0] == p and sg[1] <= yc <= sg[2] for sg in all_segs[j])

        def is_prose(t) -> bool:
            # 扫描件长行只识别了行首几个字，所以有 2 个汉字即算正文；坐标轴刻度、曲线标注一般没有汉字。
            # 分数线、负号常被 OCR 认成“一”“丨”，不算汉字
            han = [ch for ch in re.findall(r"[一-鿿]", t["text"]) if ch not in "一丨丁二十"]
            return len(han) >= 2 and not CAPTION_RE.match(t["text"].strip())

        for i, a in enumerate(anchors):
            new_segs = []
            for sg in all_segs[i]:
                p, y0, y1 = sg[0], sg[1], sg[2]
                if len(sg) > 3:
                    new_segs.append(sg)
                    continue
                gm = self.geom(p)
                bw = gm["xr"] - gm["xl"]
                lines = sorted((ln for ln in self.lines.get(p, []) if y0 <= (ln["y0"] + ln["y1"]) / 2 <= y1),
                               key=lambda ln: ln["y0"])
                caps = []
                for ln in lines:
                    m = CAPTION_RE.match(ln["text"].strip())
                    if m:
                        caps.append((ln, f"{int(m.group(1))}.{int(m.group(2))}"))
                top = y0
                for ln, owner in caps:
                    j = pid_idx.get(owner)
                    if j is None or j == i or anchors[j]["ch"] != a["ch"] or owner == a["pid"]:
                        continue
                    if owns(j, p, (ln["y0"] + ln["y1"]) / 2):
                        continue
                    # 图块上沿：图注上方最近的一行通栏正文或上一个图注之下；没有就从段顶开始
                    b_top = top
                    cx = (ln["x0"] + ln["x1"]) / 2      # 插图在图注正上方：横跨图注中线的正文行在插图之上
                    for t in lines:
                        above = is_prose(t) and (t["x0"] < cx < t["x1"] or (t["x1"] - t["x0"]) > 0.45 * bw)
                        if t["y1"] < ln["y0"] and t["y1"] > b_top and (above or CAPTION_RE.match(t["text"].strip())):
                            b_top = int(t["y1"]) + 4
                    b_bot = min(y1, int(ln["y1"]) + 6)
                    # 按行中线判断（图注下一行的上沿常和图块下沿差一两个像素，不能算进图块）
                    inside = [t for t in lines if t is not ln and b_top < (t["y0"] + t["y1"]) / 2 < b_bot]
                    prose = [t for t in inside if is_prose(t)]
                    if a["pno"] == p and b_top <= a["y0"] <= b_bot:
                        prose.append({"x0": a.get("x0", gm["xl"]), "x1": a.get("x1", gm["xl"] + 60), "text": ""})
                    if prose and not (a["pno"] == p and b_top <= a["y0"] <= b_bot) and \
                            max(t["y1"] for t in prose) < b_top + 0.4 * (b_bot - b_top):
                        b_top = int(max(t["y1"] for t in prose)) + 4
                        prose = []
                    shared = any(o is not ln and o["y0"] < b_bot and o["y1"] > b_top for o, _ in caps) or j > i
                    if not prose:
                        all_segs[j].append((p, b_top, b_bot))
                        all_flags[j].append("figure_from_" + a["pid"])
                        if shared:
                            all_flags[i].append("figure_copied_to_" + owner)
                        else:
                            if b_top - top > 12:
                                new_segs.append((p, top, b_top))
                            top = b_bot
                            all_flags[i].append("figure_moved_to_" + owner)
                        continue
                    # 图文左右混排：文字全在图注左侧（或右侧）时，只截插图那一侧
                    cl, cr = self.crop_x(p)
                    px1 = max(t["x1"] for t in prose)
                    px0 = min(t["x0"] for t in prose)
                    if px1 < ln["x0"] - 10:
                        x0, x1 = int(px1) + 8, cr
                    elif px0 > ln["x1"] + 10:
                        x0, x1 = cl, int(px0) - 8
                    else:
                        all_flags[i].append("figure_unresolved_" + owner)
                        continue
                    all_segs[j].append((p, b_top, b_bot, x0, x1))
                    all_flags[j].append("figure_from_" + a["pid"])
                    all_flags[i].append("figure_copied_to_" + owner)
                if y1 - top > 12:
                    new_segs.append((p, top, y1))
            all_segs[i] = new_segs or all_segs[i]

    def _side_figure_at(self, p: int, yb: int) -> Optional[Tuple[int, int, int]]:
        """切分线 yb 是否穿过一张排在文字右侧的插图（文字绕排）。是则返回 (图上沿, 图下沿, 图左边界)。"""
        gm = self.geom(p)
        g = self.gray(p)
        bw = gm["xr"] - gm["xl"]
        mh = gm["med_h"]
        lines = [l for l in self.body_lines(p) if l["x0"] < gm["W"] * 0.5]
        near = [l for l in lines if abs((l["y0"] + l["y1"]) / 2 - yb) < 2.2 * mh]
        short = [l for l in near if l["x1"] < gm["xl"] + 0.72 * bw]
        if not near or len(short) < len(near):
            return None                       # 切分线附近有通栏文字行：不是绕排
        xf = int(max(l["x1"] for l in short)) + 14
        if gm["xr"] - xf < 0.2 * bw:
            return None
        zone = (g[:, xf:gm["xr"]] < INK_THRESH).sum(axis=1) >= 2
        # 通栏文字行要伸进插图区一大截才算（行尾粘上插图左缘的标注“Cb”之类只多出几十像素）
        wide = [l for l in lines if l["x1"] > xf + 0.3 * (gm["xr"] - xf)]

        def blocked(y):
            return any(l["y0"] - 2 <= y <= l["y1"] + 2 for l in wide)

        def expand(step):
            y, last, gap = yb, None, 0
            while gm["header_cut"] < y < gm["footer_cut"] and not blocked(y):
                if zone[y]:
                    last, gap = y, 0
                else:
                    gap += 1
                    if gap > 3 * mh:
                        break
                y += step
            return last
        top, bot = expand(-1), expand(1)
        if top is None or bot is None or top > yb - 0.5 * mh or bot < yb + 0.5 * mh:
            return None
        if bot - top < 4 * mh:
            return None
        return int(top) - 4, int(bot) + 4, xf - 6

    def _split_side_figures(self, anchors: List[Dict[str, Any]], all_segs: List[list], all_flags: List[list]):
        """文字绕排的右侧插图跨了两道（或多道）题：插图整块归图注所属的题（没有图注的不处理），
        各题在插图那几行只保留左侧文字。"""
        pid_idx = {a["pid"]: k for k, a in enumerate(anchors)}
        done = []
        for i in range(len(anchors) - 1):
            nxt = anchors[i + 1]
            if nxt["ch"] != anchors[i]["ch"]:
                continue
            p = nxt["pno"]
            yb = self._boundary_above(p, nxt["y0"])
            if any(q == p and f0 <= yb <= f1 for q, f0, f1 in done):
                continue
            fig = self._side_figure_at(p, yb)
            if not fig:
                continue
            f0, f1, xf = fig
            done.append((p, f0, f1))
            hit = [k for k in range(len(anchors))
                   if any(len(sg) == 3 and sg[0] == p and sg[1] < f1 and sg[2] > f0 for sg in all_segs[k])]
            if len(hit) < 2:
                continue
            mh = self.geom(p)["med_h"]
            caps, odd = [], False
            for ln in self.lines.get(p, []):
                t = (ln.get("text") or "").strip()
                if not (ln["x1"] > xf and f0 - 10 <= ln["y0"] <= f1 + 3 * mh):
                    continue
                m = CAPTION_RE.match(t)
                if m and ln["x0"] >= xf - 10:
                    caps.append((f"{int(m.group(1))}.{int(m.group(2))}", ln))
                elif ln["y0"] <= f1 and (parse_anchor_candidates(t) and ln["x0"] < xf
                                         or re.search(r"\(\s*[PpＰ]?\s*\d{1,2}\s*[.．]\s*\d{1,3}\s*-\s*\d\s*\)", t)):
                    odd = True          # 区域里有题号或带编号的公式：不是单纯一张插图
            # 只处理“区域里恰好一张带图注的插图”；没有图注（多半是行尾公式）、多张图、混着别的内容都不动
            if odd or len({c for c, _ in caps}) != 1:
                continue
            owner = pid_idx.get(caps[0][0])
            if owner is None:
                continue
            f1 = max(f1, int(caps[0][1]["y1"]) + 6)
            other = [ln for ln in self.lines.get(p, []) if f0 - 10 <= ln["y0"] <= f1 and ln is not caps[0][1]
                     and CAPTION_RE.match((ln.get("text") or "").strip())]
            if other:
                continue          # 上下还挨着别的插图：分不清边界，不动
            if any(sg[0] == p and len(sg) > 3 and sg[1] < f1 and sg[2] > f0
                                        and sg[2] - sg[1] >= 3 * mh for sg in all_segs[owner]):
                continue      # 插图归位已经把整张图给了所属题：不再改动
            if owner not in hit:
                # 图注指向别的题（如前一题的插图排到了这里）：从这几题里去掉；所属题还没有这张图就补给它
                # 插图归位时可能只补了一小条（图注那一截）：换成整张插图
                all_segs[owner] = [sg for sg in all_segs[owner]
                                   if not (sg[0] == p and len(sg) > 3 and sg[1] < f1 and sg[2] > f0)]
                all_segs[owner].append((p, f0, f1, xf, self.crop_x(p)[1]))
                all_flags[owner].append("side_figure")
            cl, cr = self.crop_x(p)
            for k in hit:
                new = []
                for sg in all_segs[k]:
                    if len(sg) == 3 and sg[0] == p and sg[1] < f1 and sg[2] > f0:
                        s0, s1 = sg[1], sg[2]
                        if f0 - s0 > 8:
                            new.append((p, s0, f0))
                        new.append((p, max(s0, f0), min(s1, f1), cl, xf))
                        if k == owner and owner in hit:
                            new.append((p, f0, f1, xf, cr))
                        if s1 - f1 > 8:
                            new.append((p, f1, s1))
                    else:
                        new.append(sg)
                all_segs[k] = new
                all_flags[k].append("side_figure" if k == owner else "side_figure_removed")
            hit = hit if owner in hit else hit + [owner]
            self.log(f"    第{p + 1}页右侧插图跨 {[anchors[k]['pid'] for k in hit]}，归 {anchors[owner]['pid']}")

    def _render_all(self, anchors: List[Dict[str, Any]], page_flag: Dict[int, str]) -> Dict[str, List[Dict[str, Any]]]:
        by_ch: Dict[str, List[Dict[str, Any]]] = {}
        saved_pages = set()
        all_segs, all_flags = [], []
        for i in range(len(anchors)):
            s, f = self._segments_for(i, anchors, page_flag)
            all_segs.append(s)
            all_flags.append(f)
        self._reassign_figures(anchors, all_segs, all_flags)
        self._split_side_figures(anchors, all_segs, all_flags)
        # 人工在“切题检查”页调整过的切片：直接用保存的框，不再自动推断
        overrides = {}
        ov = self.out_root / "crop_overrides.json"
        if ov.exists():
            try:
                overrides = json.loads(ov.read_text(encoding="utf-8"))
            except Exception:
                overrides = {}
        self._masks = {}
        for i, a in enumerate(anchors):
            if a["pid"] in overrides:
                ent = overrides[a["pid"]]
                bxs, msk = (ent.get("boxes", []), ent.get("masks", [])) if isinstance(ent, dict) else (ent, [])
                all_segs[i] = [(b[0] - 1, b[1], b[2], b[3], b[4]) for b in bxs]
                all_flags[i] = ["manual"]
                self._masks[a["pid"]] = msk
        for i, a in enumerate(anchors):
            segs, flags = all_segs[i], all_flags[i]
            img = self._render_segments(segs, exact="manual" in flags)
            if img is not None and getattr(self, "_masks", {}).get(a["pid"]):
                img = self._apply_masks_to(img, segs, self._masks[a["pid"]])
            ch_dir = self.out_root / f"Chapter_{a['ch']:02d}"
            pages_dir = ch_dir / "pages"
            pages_dir.mkdir(parents=True, exist_ok=True)
            (ch_dir / "slots").mkdir(exist_ok=True)
            slice_name = f"problem_{a['pid']}_slice.png"
            if img is not None:
                img.save(str(pages_dir / slice_name))
                gmain = self.geom(a["pno"])
                if img.height < 30:
                    flags.append("slice_too_short")
                if img.height > 1.8 * gmain["H"]:
                    flags.append("slice_too_tall")
            else:
                flags.append("slice_empty")
            if a["score"] < 0.8:
                flags.append("low_ocr_confidence")
            for p in {sg[0] for sg in segs}:
                if p not in saved_pages:
                    pix = self.doc[p].get_pixmap(dpi=self.dpi)
                    pix.save(str(pages_dir / f"page_{p + 1}.png"))
                    saved_pages.add(p)
            by_ch.setdefault(str(a["ch"]), []).append({
                "problem_id": a["pid"],
                "text": self._text_for(segs),
                "page": a["pno"] + 1,
                "pages": sorted({sg[0] + 1 for sg in segs}),
                "slice": slice_name,
                "boxes": list(getattr(self, "_last_boxes", [])) if img is not None else [],
                "masks": list(getattr(self, "_masks", {}).get(a["pid"], [])),
                "flags": flags,
                "gap_before": a["gap_before"],
            })

        return by_ch

    def _finish_report(self, by_ch: Dict[str, List[Dict[str, Any]]], report: Dict[str, Any]):
        for ch, probs in by_ch.items():
            ch_dir = self.out_root / f"Chapter_{int(ch):02d}"
            src = "text_layer" if self.text_layer else "none"
            # 续跑时保留已通过检验的题干（同题号、同页），避免重复转写与核对
            old_items = {}
            pj_old = ch_dir / "problems.json"
            if pj_old.exists():
                try:
                    old_items = {str(o["problem_id"]): o for o in json.loads(pj_old.read_text(encoding="utf-8"))}
                except Exception:
                    old_items = {}
            out_items = []
            for p in probs:
                item = dict({k: v for k, v in p.items() if k in ("problem_id", "text", "page", "boxes", "masks")}, text_source=src)
                o = old_items.get(p["problem_id"])
                # 同题同页时保留已有题干（任何来源），只有新切出的题才留空等待转写；绝不把已有题干清空
                if o and o.get("page") == p["page"] and len((o.get("text") or "").strip()) >= 8:
                    item["text"], item["text_source"] = o["text"], o.get("text_source", "none")
                    if o.get("import_warn"):
                        item["import_warn"] = o["import_warn"]
                    if "has_fig" in o:
                        item["has_fig"] = o["has_fig"]
                out_items.append(item)
            (ch_dir / "problems.json").write_text(json.dumps(
                out_items,
                ensure_ascii=False, indent=2), encoding="utf-8")
            ks = [int(p["problem_id"].split(".")[1]) for p in probs]
            missing = sorted(set(range(1, max(ks) + 1)) - set(ks))
            report["chapters"][ch] = {
                "count": len(probs), "last_index": max(ks), "missing": missing,
                "first_page": min(p["page"] for p in probs),
                "last_page": max(max(p["pages"]) for p in probs),
                "flagged": {p["problem_id"]: p["flags"] for p in probs if p["flags"]},
            }
        (self.out_root / "crop_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self._update_profile(report)
        self._print_summary(report)
        return by_ch

    def _update_profile(self, report: Dict[str, Any]):
        pf = self.out_root / "profile.json"
        try:
            cfg = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {"book": {}, "chapters": {}}
        except Exception:
            cfg = {"book": {}, "chapters": {}}
        old = cfg.get("chapters", {}) or {}
        titles = self.chapter_titles()
        new = {}
        for ch in sorted(report["chapters"], key=int):
            info = report["chapters"][ch]
            if self.exam and int(ch) in self.section_names:
                nm = self.section_names[int(ch)]
            else:
                nm = (f"第{ch}章 {titles[ch]}" if ch in titles else None) or old.get(ch, {}).get("name") or f"第 {ch} 章"  # 页眉实测章名优先于 AI 猜测
            new[ch] = {"name": nm, "start_page": info["first_page"], "end_page": info["last_page"],
                       "problem_count": info["count"]}
        cfg["chapters"] = new
        cfg.setdefault("book", {})["doc_type"] = "exam" if self.exam else "book"
        pf.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _print_summary(self, report: Dict[str, Any]):
        self.log("=" * 60)
        total = 0
        for ch in sorted(report["chapters"], key=int):
            r = report["chapters"][ch]
            total += r["count"]
            miss = f" 缺号 {r['missing']}" if r["missing"] else ""
            flg = f" 标记 {len(r['flagged'])} 题" if r["flagged"] else ""
            self.log(f"  第{ch}章: {r['count']} 题 (1~{r['last_index']}) 页 {r['first_page']}-{r['last_page']}{miss}{flg}")
        self.log(f"[OK] 共切出 {total} 题，质检报告: {self.out_root / 'crop_report.json'}")
