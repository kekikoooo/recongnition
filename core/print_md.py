# -*- coding: utf-8 -*-
"""
打印版 Markdown 生成（移植自 test1 的 generate_print_md.py，沿用其中调教过的排版规则）：
把各章 Compact 速查整理成便携打印版：去 Emoji、规范习题标题与步骤换行、合并零碎短行、清除横线与标点瑕疵。
输入的题号标题用 test1 的“## 习题 X.X”形式；test2 的“### 第 X 题”先用 to_exercise_headings 转换。
"""
import re
import textwrap

CONTINUATION_STARTS = (
    '已知', '代入', '代回', '因此', '从而', '其中', '即', '此时', '故',
    '两方法', '结果为', '两数相除', '两边', '积分得', '整理得', '求和得',
    '式中', '分母', '分子', '实部', '虚部', '由于', '同理可得', '与上式相同',
    '根据欧拉公式', '由欧拉公式', '利用欧拉公式', '利用周期性', '利用', '定义',
    '综上所述', '解得', '化简得', '可得'
)

# 步骤/列表独立行识别正则：遇到步骤、第一步、第二步、1.、2.、(1)、(2)、（1）、（2）、①、②等，绝不合并，强制开启独立段落换行！
IS_STEP_START = re.compile(
    r'^(?:'
    r'\*\*(?:步骤|第一|第二|第三|第四|第五|第六|第七|第八|第九|第十|方法|解法|情况|阶段|分析|视角|小题|算子|特性).*?\*\*'
    r'|\*\*[（\(][0-9a-zA-Z一二三四五六七八九十]+[）\)].*?\*\*'
    r'|[（\(][0-9a-zA-Z一二三四五六七八九十]+[）\)]\s*'
    r'|[①②③④⑤⑥⑦⑧⑨⑩]\s*'
    r'|\d+\.\s+'
    r'|\*\*【(?:最终结论|考研精要|波形标注|规范答案)】\*\*'
    r'|[-*]\s+'
    r')'
)

def clean_and_stream_print_safe(raw_text: str, ch: int, book_title: str = "") -> str:
    # =========================================================================
    # 步骤 0: 严密抽取并保护所有代码块（波形 ASCII 示意图、框图、代码），绝不损坏字符！
    # =========================================================================
    saved_blocks = []
    def extract_block(m):
        content = m.group(1).strip()
        if not content:
            return ''
            
        lines = content.splitlines()
        lang = 'text'
        if lines and lines[0].strip() in ('text', 'python', 'mermaid', 'c', 'cpp', 'matlab'):
            lang = lines[0].strip()
            lines = lines[1:]
            
        inner = '\n'.join(lines)
        inner_dedent = textwrap.dedent(inner).strip()
        if not inner_dedent:
            return ''
            
        condensed_lines = []
        prev_blank = False
        for l in inner_dedent.splitlines():
            is_blank = not l.strip()
            if is_blank and prev_blank:
                continue
            condensed_lines.append(l.rstrip())
            prev_blank = is_blank
            
        compact_code = '\n'.join(condensed_lines)
        block_str = f'\n\n```{lang}\n{compact_code}\n```\n\n'
        idx = len(saved_blocks)
        saved_blocks.append(block_str)
        return f'\n\n__SAFE_BLOCK_PLACEHOLDER_{idx}__\n\n'

    text = re.sub(r'```(.*?)```', extract_block, raw_text, flags=re.DOTALL)

    # =========================================================================
    # 步骤 1: 彻底清理所有 Emoji（防止 XeLaTeX 抛 Missing Character 甚至 hPutChar 崩溃）并规范章首
    # =========================================================================
    # 移除所有 4 字节 Unicode Emoji 及常见特殊符号
    text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
    for em in ['📝', '🌟', '💡', '🎯', '⚡', '📌', '🚀', '📘', '🖨️', '🔹', '✓', '🔍', '🔬', '🌿', '⚠️', '❌', '✅']:
        text = text.replace(em, '')

    title_line = f'# {book_title or "题库"} - 第 {ch} 章 核心考点与习题全解（便携打印版）\n\n'
    first_ex_match = re.search(r'\n(?=#{2,3}\s*习题\s*[\d\.]+)', text)
    if first_ex_match:
        text = title_line + text[first_ex_match.start():].lstrip()
    else:
        text = re.sub(r'^#.*?\n', title_line, text, count=1)

    # =========================================================================
    # 步骤 2: 规范习题标题与彻底清除重复占位标语（杜绝 VS Code Duplicate Identifier 警告）
    # =========================================================================
    # 2.1 规范习题主标题：去除多余后缀，精准保留题号与子题序号如 1.42(c)
    text = re.sub(r'^#{2,3}\s*习题\s*([\d\.]+(?:\([a-zA-Z0-9]+\))?).*$', r'## 习题 \1', text, flags=re.MULTILINE)

    # 2.2 彻底删除重复占位字条
    text = re.sub(
        r'^#{2,4}\s*(?:详尽分步推导|分步推导|最终结论与物理意义|最终结论|物理意义|核心推导与最终答案|核心步骤与最终答案|核心推导与答案速查|推导与答案速查|核心步骤|推导与答案)\s*$\n?',
        '',
        text,
        flags=re.MULTILINE
    )

    # 2.3 将结论与物理意义等重复副标题统一转换为行内加粗前缀（非 Markdown 标题，不占行且绝不产生重复 ID 警告）
    text = re.sub(
        r'^#{3,5}\s*(?:\d+\.\s*)?(?:规范答案|规范解析式与答案|最终解析表达式|最终解析结论|最终解析解|最终解析式|最终计算结论|最终数学结论|最终结论|最终结果汇总|最终表达式|解析表达式|答案速查|标准答案|规范答案与结论|结论).*$',
        r'**【最终结论】**',
        text,
        flags=re.MULTILINE
    )
    text = re.sub(
        r'^#{3,5}\s*(?:\d+\.\s*)?(?:物理意义与考研精要总结|核心考点与物理意义|物理意义与考点总结|考研精要总结|考研精要|物理意义阐释|物理意义分析|物理意义与系统特性解析|系统物理意义深度剖析|物理意义).*$',
        r'**【考研精要】**',
        text,
        flags=re.MULTILINE
    )
    text = re.sub(
        r'^#{3,5}\s*(?:\d+\.\s*)?(?:模特性波形草图标注|极点分布草图|波形草图|波形与频谱标注).*$',
        r'**【波形标注】**',
        text,
        flags=re.MULTILINE
    )

    # 2.4 将所有 5 级及以上标题（如 ##### 步骤 1：...）转为加粗正文，避免过多无意义标题层级
    text = re.sub(r'^#{5,6}\s*(.*)$', r'**\1**', text, flags=re.MULTILINE)

    # =========================================================================
    # 步骤 3: 彻底清除所有 --- 横线（杜绝产生莫名 — 破折号与 Setext 巨大字体）与公式归一化
    # =========================================================================
    # 彻底移除所有 --- 横线，无论是题内还是题尾，完全靠 ## 习题 X.X 自然分页分隔！
    text = re.sub(r'^\s*---\s*$\n?', '', text, flags=re.MULTILINE)

    # 替换大号数学符号，归一为标准行内比例：\dfrac -> \frac, 去除 \displaystyle
    text = text.replace(r'\dfrac', r'\frac').replace(r'\displaystyle', '')
    # 将 inline math 中非法的 \tag{...} 转换为合规的 \quad \text{(...)}，防止 amsmath 抛出 "\tag not allowed here" 致命错误
    text = re.sub(r'\\tag\{([^}]+)\}', r'\\quad \\text{(\1)}', text)

    # 将所有块级 $$ ... $$ 转换为行内 $ ... $
    def replace_block_math(match):
        raw_eq = match.group(1).strip()
        lines = [l.strip() for l in raw_eq.splitlines() if l.strip()]
        compact_eq = ' '.join(lines)
        compact_eq = re.sub(r'\s{2,}', ' ', compact_eq)
        return f'${compact_eq}$'
        
    # 按题单独配对 $$：整章一次性配对时，某一题里有一个没配对的 $$（或 $a$$b$ 这种紧贴写法），
    # 之后所有配对整体错位，会把正文和后面几十道题的标题都压成一个“公式”（实测整章从 71 题丢到 22 题）
    def _per_problem(part):
        return re.sub(r'\$\$(.*?)\$\$', lambda m: m.group(0) if len(m.group(1)) > 3000 else replace_block_math(m), part, flags=re.DOTALL)
    text = ''.join(_per_problem(part) for part in re.split(r'(?m)^(?=#{2,3}[ ]*习题)', text))

    # =========================================================================
    # 步骤 4: 按照小节（# 标题）智能流式合并：步骤强制换行，公式与已知连写！
    # =========================================================================
    sections = re.split(r'(\n(?=#{1,4}\s+))', text)
    new_sections = []
    
    for sec in sections:
        if not sec.strip():
            continue
        lines = [l.strip() for l in sec.strip().splitlines() if l.strip()]
        if not lines:
            continue
            
        header = lines[0] if lines[0].startswith('#') else None
        body_lines = lines[1:] if header else lines
        
        compact_body = []
        for line in body_lines:
            # 严格保护：占位符绝不与任何文本合并
            if '__SAFE_BLOCK_PLACEHOLDER_' in line:
                compact_body.append(line)
                continue
                
            if not compact_body:
                compact_body.append(line)
                continue
                
            prev = compact_body[-1]
            # 合并后的行已经很长就不再往里并：test1 的合并规则会连锁（行尾是冒号/句号就继续并下一行），
            # 实测一道题触发后把后面几十道题的标题和正文都吞进同一行（11 万字符，PDF 编译失败、内容丢失）
            if len(prev) > 1200:
                compact_body.append(line)
                continue
            # 表格行（| ... |）不合并：test1 的“合并零碎短行”会把整张表压成一行（实测 11 万字符的超长行，PDF 编译失败）
            if line.lstrip().startswith('|') or prev.lstrip().startswith('|'):
                if line.lstrip().startswith('|') and not prev.lstrip().startswith('|') and prev.strip():
                    compact_body.append('')      # 表格前要有空行，pandoc 才认作表格
                compact_body.append(line)
                continue
            
            # 如果上一行是代码块占位符，绝不合并，保持独立块隔离！
            if '__SAFE_BLOCK_PLACEHOLDER_' in prev:
                compact_body.append(line)
                continue

            # 核心原则：如果当前行是“新步骤”或“编号列表”（步骤 1、第一步、1.、2.、(a)等），强制换行，绝不合并！
            if IS_STEP_START.match(line):
                compact_body.append(line)
                continue
            
            # 规则 1: 上一行以冒号结尾，当前行是公式或推导，直接连在冒号后面不换行！
            if prev.endswith(('：', ':')) and not line.startswith('__SAFE_BLOCK_PLACEHOLDER_'):
                compact_body[-1] = prev + ' ' + line
                continue
                
            # 规则 2: 当前行以 已知 等开头，不换行，连在上一句后面！
            if line.startswith(CONTINUATION_STARTS):
                sep = '； ' if not prev.endswith(('。', '；', '，', '：', ':')) else ' '
                compact_body[-1] = prev + sep + line
                continue
                
            # 规则 3: 上一行是方法/步骤标题（行内未带冒号），连在后面
            if re.match(r'^\*\*(?:方法|步骤|情况|解法).*?\*\*[：:]?$', prev):
                sep = ' ' if prev.endswith(('：', ':')) else '： '
                compact_body[-1] = prev + sep + line
                continue
                
            # 规则 4: 结论句连在后面
            if line.endswith(('完全一致。', '相同。', '成立。', '证毕。')):
                sep = '； ' if not prev.endswith(('。', '；', '，', '：', ':')) else ' '
                compact_body[-1] = prev + sep + line
                continue
                
            # 规则 5: 如果上一行很短且不是列表/标题/占位符，合并连写（但 line 不能是步骤）
            if len(prev) < 35 and not prev.startswith(('-', '*')) and not line.startswith(('-', '*')):
                sep = ' ' if prev.endswith(('，', '。', '；', '：')) else '； '
                compact_body[-1] = prev + sep + line
                continue
                
            # 规则 6: 如果连续是多个超短列表采样点，合并为一行
            m_prev_item = re.match(r'^-\s*(?:采样值[：:])?\s*(\$[a-zA-Z0-9_\[\]\(\)\-\+\*]+\s*=\s*[^$]+\$)$', prev)
            m_curr_item = re.match(r'^-\s*(\$[a-zA-Z0-9_\[\]\(\)\-\+\*]+\s*=\s*[^$]+\$)$', line)
            if m_prev_item and m_curr_item:
                compact_body[-1] = prev + ', ' + m_curr_item.group(1)
                continue

            compact_body.append(line)
            
        res_body = []
        for item in compact_body:
            if not res_body:
                res_body.append(item)
            else:
                if IS_STEP_START.match(item):
                    res_body.append('\n' + item)
                else:
                    res_body.append(item)
        sec_text = (header + '\n\n' if header else '') + '\n'.join(res_body)
        new_sections.append(sec_text)
        
    res = '\n\n'.join(new_sections) + '\n'

    # 强制保障：所有（1）、（2）、(1)、(2)、步骤 1 等标号前，必须拥有标准段落空行，绝不被紧贴挤压！
    res = re.sub(r'([^\n])\n(\*\*[（\(][0-9a-zA-Z一二三四五六七八九十]+[）\)].*?\*\*)', r'\1\n\n\2', res)
    res = re.sub(r'([^\n])\n([（\(][0-9a-zA-Z一二三四五六七八九十]+[）\)])', r'\1\n\n\2', res)
    res = re.sub(r'([^\n])\n(\*\*(?:步骤|第一|第二|第三|第四|第五|方法|解法).*?\*\*)', r'\1\n\n\2', res)
    res = re.sub(r'([^\n])\n(\d+\.\s+)', r'\1\n\n\2', res)
    res = re.sub(r'([^\n])\n(\*\*【(?:最终结论|考研精要|波形标注)】\*\*)', r'\1\n\n\2', res)

    # 规范大题与小题之间的空行，确保 ## 习题 X.X 前后清晰干净，无任何横线
    res = re.sub(r'(\n{2,})(##\s*习题\s*[\d\.]+)', r'\n\n\2', res)

    # =========================================================================
    # 步骤 5: 完整还原受保护的代码块（ASCII 缩小版）与最终标点清理
    # =========================================================================
    for idx, blk in enumerate(saved_blocks):
        placeholder = f'__SAFE_BLOCK_PLACEHOLDER_{idx}__'
        res = res.replace(placeholder, blk.strip())
        
    # 清理多余的双冒号与标点瑕疵
    res = re.sub(r'([：:])\s*\*\*([：:])', r'\1**', res)
    res = re.sub(r'：\*\*[:：]', '：** ', res)
    res = re.sub(r'：\*\*；', '：** ', res)
    res = re.sub(r'：\s*；', '：', res)
    res = re.sub(r'\n{3,}', '\n\n', res)
    return res



def to_exercise_headings(compact_md: str) -> str:
    """test2 的速查里每题标题是“### 第 3.12 题”，转成 test1 打印规则认识的“## 习题 3.12”"""
    return re.sub(r"(?m)^#{2,4}\s*第\s*([\d.]+(?:\([a-zA-Z0-9]+\))?)\s*题\s*$", r"## 习题 \1", compact_md)


def _balance_fences(md: str) -> str:
    """按题检查代码围栏 ``` 是否成对：模型偶尔漏写收尾的 ```，整章一次性配对时会整体错位，
    把后面所有题的正文都当成代码块压成一行（实测出现过 11 万字符的超长行，LaTeX 直接编译失败）。
    没配对的题，在这一题末尾补一个收尾围栏，错位就只限在这一题内。"""
    parts = re.split(r"(?m)^(?=## 习题 )", md)
    out = []
    for p in parts:
        if p.count("```") % 2 == 1:
            p = p.rstrip() + "\n```\n\n"
        out.append(p)
    return "".join(out)


def _light_clean(sec: str) -> str:
    """整理规则对某道题失效时的退路：只去 Emoji 和多余空行，其余原样保留"""
    sec = re.sub(r"[\U00010000-\U0010ffff]", "", sec)
    return re.sub(r"\n{3,}", "\n\n", sec).rstrip() + "\n\n"


def build_print_md(compact_md: str, ch: int, book_title: str = "", max_line: int = 5000) -> str:
    """打印版 = test1 的整理规则；但整理后某道题出现超长行（>max_line 字符，说明合并规则把表格/图示压坏了，
    且 TeX 单行过长会让整本 PDF 编译失败）时，这一题退回只去 Emoji 的轻整理，保证 PDF 一定能编出来。"""
    raw = _balance_fences(to_exercise_headings(compact_md))
    out = clean_and_stream_print_safe(raw, ch, book_title)
    split_re = r"(?m)^(?=## 习题 )"
    cleaned = re.split(split_re, out)
    if all(len(l) <= max_line for l in out.split("\n")):
        return out
    raw_secs = {p.split("\n", 1)[0].strip(): p for p in re.split(split_re, raw)[1:]}
    fixed = [cleaned[0]]
    for sec in cleaned[1:]:
        title = sec.split("\n", 1)[0].strip()
        if any(len(l) > max_line for l in sec.split("\n")) and title in raw_secs:
            sec = _light_clean(raw_secs[title])
        fixed.append(sec)
    return "".join(fixed)
