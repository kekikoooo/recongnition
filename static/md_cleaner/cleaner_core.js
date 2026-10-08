/*
 * cleaner_core.js —— 清洗核心 (浏览器 / Node 通用)
 * clean(rawText, opts, katex) -> { result, report }
 *   opts: { reorder, noise, tag }   (默认全部 true)
 *   katex: 可选，传入后会做公式渲染校验
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MdCleaner = factory();
})(typeof self !== 'undefined' ? self : this, function () {

  const P = '，。：；？！、';
  const reLead = new RegExp('^\\s*([' + P + ']+)\\s*');
  const reTrail = new RegExp('\\s*([' + P + ']+)\\s*$');
  const reMid = new RegExp('[' + P + ']');
  const MAP = { '，': ',\\ ', '：': ':', '；': ';', '。': '.', '？': '?', '！': '!', '、': ',\\ ' };
  const ENV = /\\begin\{(aligned|align\*?|cases|array|pmatrix|bmatrix|vmatrix|matrix|gathered|split)\}/;

  // $ 之间不允许出现空行；\$ 视为普通字符
  function tokenize(src) {
    const segs = [], problems = [];
    let i = 0, n = src.length, buf = '';
    const flush = () => { if (buf) { segs.push({ t: 'text', s: buf }); buf = ''; } };
    while (i < n) {
      const c = src[i];
      if (c === '\\' && src[i + 1] === '$') { buf += '\\$'; i += 2; continue; }
      if (c === '$') {
        if (src[i + 1] === '$') {
          const j = src.indexOf('$$', i + 2);
          if (j < 0) { problems.push('$$ 未闭合'); buf += src.slice(i); i = n; continue; }
          flush(); segs.push({ t: 'disp', s: src.slice(i + 2, j) }); i = j + 2; continue;
        }
        let j = i + 1, found = -1;
        while (j < n) {
          if (src[j] === '\\') { j += 2; continue; }
          if (src[j] === '\n' && src[j + 1] === '\n') break;
          if (src[j] === '$') { found = j; break; }
          j++;
        }
        if (found < 0) { problems.push('$ 未闭合'); buf += '$'; i++; continue; }
        flush(); segs.push({ t: 'inl', s: src.slice(i + 1, found) }); i = found + 1; continue;
      }
      buf += c; i++;
    }
    flush();
    return { segs, problems };
  }

  function splitBlocks(src) {
    const k = src.search(/^### /m);
    if (k < 0) return { head: '', blocks: [src] };
    return { head: src.slice(0, k), blocks: src.slice(k).split(/^(?=### )/m) };
  }
  const headOf = b => (b.startsWith('### ') ? b.split('\n')[0].trim() : '(文件开头)');
  const sig = s => s.replace(/&emsp;|\s/g, '').split('').sort().join('');

  function clean(rawText, opts, katex) {
    opts = Object.assign({ reorder: true, noise: true, tag: true }, opts || {});
    const crlf = /\r\n/.test(rawText);
    let text = rawText.replace(/^﻿/, '').replace(/\r\n/g, '\n');
    const report = { fixed: {}, removed: [], unbalanced: [], remaining: [], notes: [], katex: !!katex };
    const bump = k => { report.fixed[k] = (report.fixed[k] || 0) + 1; };

    // ---- 1. 模型草稿
    function stripNoise(src) {
      const lines = src.split('\n'), out = [];
      const hasCJK = l => /[㐀-鿿]/.test(l);
      const mostlyEnglish = l => {
        const t = l.replace(/\$\$[\s\S]*?\$\$/g, ' ').replace(/\$[^$]*\$/g, ' ');
        return (t.match(/[A-Za-z]{2,}/g) || []).length >= 4 && (t.match(/[㐀-鿿]/g) || []).length < 12;
      };
      const isNoise = l => /^\s*!\[[^\]]*\]\(\s*image_placeholder[^)]*\)\s*$/.test(l)
        || /^\s*\(?\*{0,2}Note:?\*{0,2}/i.test(l)
        || /^\s*\((The problem includes|Note:)/.test(l)
        || /^\s*(Let's|Let me|I will|I'll|Wait,|Actually,|These descriptions)\b/.test(l);
      let head = '';
      for (let i = 0; i < lines.length; i++) {
        if (lines[i].startsWith('### ')) head = lines[i].trim();
        if (isNoise(lines[i])) {
          const start = i;
          while (i + 1 < lines.length && lines[i + 1].trim() && !lines[i + 1].startsWith('### ')
            && (!hasCJK(lines[i + 1]) || /^\s*[-*]\s/.test(lines[i + 1]) || mostlyEnglish(lines[i + 1]))
            && !/^\s*\$\$\s*$/.test(lines[i + 1])) i++;
          report.removed.push(`${head} 第${start + 1}行起共${i - start + 1}行: ${lines[start].trim().slice(0, 50)}`);
          continue;
        }
        out.push(lines[i]);
      }
      return out.join('\n');
    }

    // ---- 2. \[..\] \(..\)
    function fixBracketDelims(src) {
      src = src.replace(/\\\[([\s\S]*?)\\\]/g, (m, b) => { bump('\\[..\\] 改成 $$..$$'); return '\n$$\n' + b.trim() + '\n$$\n'; });
      src = src.replace(/(?<!\\)\\\(([\s\S]*?)\\\)/g, (m, b) => { bump('\\(..\\) 改成 $..$'); return '$' + b.trim() + '$'; });
      return src;
    }

    // ---- 3. 公式内容
    function wrapCJK(s) {
      let out = '', i = 0;
      while (i < s.length) {
        const m = /^\\(text|textbf|textit|mathrm|mbox|operatorname)\s*\{/.exec(s.slice(i));
        if (m) {
          let depth = 1, j = i + m[0].length;
          while (j < s.length && depth) { if (s[j] === '{') depth++; else if (s[j] === '}') depth--; j++; }
          out += s.slice(i, j); i = j; continue;
        }
        const r = /^[㐀-鿿]+/.exec(s.slice(i));
        if (r) { out += '\\text{' + r[0] + '}'; bump('公式内的中文字包进 \\text{}'); i += r[0].length; continue; }
        out += s[i++];
      }
      return out;
    }

    function fixMath(block) {
      const { segs, problems } = tokenize(block);
      if (problems.length) { report.unbalanced.push(headOf(block) + ' ' + problems.join('、')); return block; }
      let out = '';
      segs.forEach((sg0, k) => {
        let sg = sg0;
        if (sg.t === 'text') { out += sg.s; return; }
        // `$a$$b$` 两个行内公式贴在一起会被当成 $$ —— 中间补空格 (并提示，原文可能丢了文字)
        if (sg.t === 'inl' && k > 0 && segs[k - 1].t === 'inl') { out += ' '; bump('相邻的两个行内公式贴在一起，补了空格 (请核对原文)'); }
        // 全角/宽空格在公式里会报警告；行内公式里的 \tag 会报错
        if (/[ -​　]/.test(sg.s)) { sg = { t: sg.t, s: sg.s.replace(/[ -​　]+/g, ' \\quad ') }; bump('公式内的宽空格换成 \\quad'); }
        if (sg.t === 'inl' && opts.tag && /\\tag\{/.test(sg.s)) {
          sg = { t: 'inl', s: sg.s.replace(/[ \t]*\\tag\{([^}]*)\}/g, (mm, x) => { bump('\\tag 改成 \\qquad\\text{()}'); return ' \\qquad \\text{(' + x.replace(/^\((.*)\)$/, '$1') + ')}'; }) };
        }
        let s = sg.s, pre = '', post = '', m;
        if (sg.t === 'inl' && ENV.test(s)) {
          const nx = segs[k + 1] && segs[k + 1].t === 'text' ? segs[k + 1].s : '';
          const ln = out.slice(out.lastIndexOf('\n') + 1) + nx.split('\n')[0];
          if (!/&emsp;/.test(ln)) { sg = { t: 'disp', s }; bump('行内多行环境提成块级公式'); }
        }
        while ((m = reLead.exec(s))) { pre += m[1]; s = s.slice(m[0].length); bump('公式首部的中文标点移出'); }
        while ((m = reTrail.exec(s))) { post = m[1] + post; s = s.slice(0, m.index); bump('公式尾部的中文标点移出'); }
        if (sg.t === 'inl') {
          if (reMid.test(s)) {
            const parts = s.split(new RegExp('([' + P + ']+)'));
            let acc = '';
            parts.forEach((p, i) => { if (i % 2 === 0) { if (p.trim()) acc += '$' + wrapCJK(p.trim()) + '$'; } else acc += p; });
            bump('公式中间的中文标点拆开'); out += pre + acc + post; return;
          }
          out += pre + '$' + wrapCJK(s) + '$' + post; return;
        }
        if (reMid.test(s)) { s = s.replace(new RegExp('([' + P + '])', 'g'), x => MAP[x]); bump('块级公式内中文标点转半角'); }
        s = wrapCJK(s.trim());
        if (opts.tag && /\\tag\{/.test(s)) {
          s = s.replace(/[ \t]*\\tag\{([^}]*)\}/g, (mm, x) => { bump('\\tag 改成 \\qquad\\text{()}'); return ' \\qquad \\text{(' + x.replace(/^\((.*)\)$/, '$1') + ')}'; });
        }
        out = out.replace(/[ \t]+$/, '');
        if (pre) out += pre;
        if (out.length && !out.endsWith('\n\n')) out += out.endsWith('\n') ? '\n' : '\n\n';
        const nxt = segs[k + 1];
        if (nxt && nxt.t === 'text') segs[k + 1] = { t: 'text', s: nxt.s.replace(/^[ \t]*\n?/, '') };
        out += '$$\n' + s + '\n$$\n\n';
        bump('块级公式规范为独占一行');
      });
      return out;
    }

    // ---- 3.5 裸露公式补全：整行是公式 -> $$ 块；中文里夹的公式片段 -> $..$
    const LATEX = /\\[a-zA-Z]+|[_^]\s*[{\w]/;
    function fixBare(block) {
      // 先把裸露的多行环境 \begin{X} ... \end{X} 整体包成 $$ 块 (不动已在 $$ 里的)
      {
        const ls = block.split('\n'), out = [];
        let inD = false;
        for (let i = 0; i < ls.length; i++) {
          if (ls[i].trim() === '$$') { inD = !inD; out.push(ls[i]); continue; }
          const m = !inD && /^\s*\\begin\{(\w+\*?)\}\s*$/.exec(ls[i]);
          if (m) {
            let j = i + 1;
            while (j < ls.length && !new RegExp('^\\s*\\\\end\\{' + m[1].replace('*', '\\*') + '\\}').test(ls[j]) && j - i < 30) j++;
            if (j < ls.length && new RegExp('^\\s*\\\\end\\{').test(ls[j])) {
              out.push('$$', ...ls.slice(i, j + 1).map(x => x.trim()), '$$'); bump('裸露的多行环境整体包成 $$'); i = j; continue;
            }
          }
          out.push(ls[i]);
        }
        block = out.join('\n');
      }
      const { segs, problems } = tokenize(block);
      if (problems.length) return block;
      return segs.map((sg, k) => {
        if (sg.t !== 'text' || !LATEX.test(sg.s.replace(/\\\$/g, ''))) return sg.s ? (sg.t === 'text' ? sg.s : sg.t === 'inl' ? '$' + sg.s + '$' : '$$' + sg.s + '$$') : '';
        const lines = sg.s.split('\n');
        return lines.map((line, j) => {
          if (!LATEX.test(line) || /^\s*###/.test(line)) return line;
          const whole = (j > 0 || k === 0) && (j < lines.length - 1 || k === segs.length - 1);
          // 屏蔽 \text{...} 里的中文
          const stash = [];
          const masked = line.replace(/\\(text|textbf|mathrm|mbox)\s*\{[^{}]*\}/g, m => { stash.push(m); return '\u0003' + (stash.length - 1) + '\u0004'; });
          const unmask = s => s.replace(/\u0003(\d+)\u0004/g, (m, i) => stash[+i]);
          const body = masked.replace(/[\s＀-￯　-〿]+$/, '');
          if (whole && !/[㐀-鿿＀-￯　-〿]/.test(body) && !/&emsp;/.test(body) && !/^\s*[（(][a-z][）)]/.test(body)) {
            bump('整行裸公式补上 $$'); return '$$\n' + unmask(body).trim() + '\n$$';
          }
          // 行内：按中文 / 全角标点 / &emsp; / 【】 切开，含 LaTeX 的非中文片段包上 $
          const parts = masked.split(/([㐀-鿿　-〿＀-￯]+|&emsp;|【[^】]*】)/);
          return parts.map((p, i) => {
            if (i % 2) return unmask(p);
            if (!LATEX.test(p)) return unmask(p);
            const lab = /^(\s*(?:\d+\.\s+)?[（(][a-z][）)]\s*)/.exec(p);
            const pre = lab ? lab[1] : '';
            const rest = p.slice(pre.length);
            if (!LATEX.test(rest)) return unmask(p);
            const m = /^(\s*)([\s\S]*?)([\s,.;:]*)$/.exec(rest);
            if (!m[2]) return unmask(p);
            bump('中文间的裸公式补上 $..$');
            return unmask(pre + m[1] + '$' + m[2] + '$' + m[3]);
          }).join('');
        }).join('\n');
      }).join('');
    }

    // ---- 4. 版式 / 重排
    const fixLabels = src => src.replace(/^(?:\d+\.\s+)?\*\*([（(][a-z][）)])\*\*/gm, (m, l) => { bump('**(a)** 改成 (a)'); return l; });

    function reorder(block) {
      if (!block.startsWith('### ')) return block;
      const nl = block.indexOf('\n'); if (nl < 0) return block;
      const h = block.slice(0, nl).trim(), body = block.slice(nl + 1);
      const { segs, problems } = tokenize(body);
      if (problems.length) return block;
      const store = [];
      const masked = segs.map(s => s.t === 'text' ? s.s : (store.push(s), '\u0001' + (store.length - 1) + '\u0002')).join('');
      const re = /(^|&emsp;&emsp;[ \t]*)[（(]([a-z])[）)]/gm;
      let labs = [], m;
      while ((m = re.exec(masked))) labs.push({ letter: m[2], start: m.index + m[1].length, sep: m.index });
      const contig = ls => { const t = [...new Set(ls)].sort(); return t.length === ls.length && t.every((c, i) => c.charCodeAt(0) === 97 + i); };
      if (!contig(labs.map(l => l.letter)) && contig(labs.filter(l => l.letter !== 'i').map(l => l.letter))) labs = labs.filter(l => l.letter !== 'i');
      if (labs.length < 2) return block;
      const letters = labs.map(l => l.letter);
      if (letters.every((c, i) => i === 0 || c > letters[i - 1])) return block;
      if (!contig(letters)) { report.notes.push(`${h} 小问顺序 ${letters.join('')} 不规则，未自动重排`); return block; }
      const pre = masked.slice(0, labs[0].sep);
      const units = labs.map((l, i) => ({ letter: l.letter, txt: masked.slice(l.start, i + 1 < labs.length ? labs[i + 1].sep : masked.length) }));
      const sorted = [...units].sort((a, b) => (a.letter < b.letter ? -1 : 1));
      const hasTail = u => /\n\s*\n\s*\S/.test(u.txt.replace(/\u0001\d+\u0002/g, '§').trim().replace(/(\s*§)+$/, ''));
      const bad = units.filter(hasTail);
      if (bad.length && (bad.length > 1 || bad[0] !== units[units.length - 1] || bad[0] !== sorted[sorted.length - 1])) {
        report.notes.push(`${h} 小问顺序 ${letters.join('')} 含穿插段落，未自动重排`); return block;
      }
      let nb = pre.replace(/[ \t]+$/, '') + sorted.map(u => u.txt.trim().replace(/(\s*&emsp;)+\s*$/, '')).join('\n') + '\n\n';
      nb = nb.replace(/\u0001(\d+)\u0002/g, (x, i) => { const s = store[+i]; return s.t === 'disp' ? '$$' + s.s + '$$' : '$' + s.s + '$'; });
      if (sig(nb) !== sig(body)) { report.notes.push(`${h} 重排校验失败，保持原样`); return block; }
      bump('小问顺序重排');
      return block.slice(0, nl + 1) + nb.replace(/\n+$/, '\n\n');
    }

    // ---- 校验
    function validate(src) {
      const { head, blocks } = splitBlocks(src);
      [head, ...blocks].forEach(b => {
        const h = b === head ? '(文件开头)' : headOf(b);
        const { segs, problems } = tokenize(b);
        problems.forEach(p => report.remaining.push(`${h}: ${p}`));
        const outside = segs.filter(s => s.t === 'text').map(s => s.s).join('');
        const bare = outside.replace(/\\\$/g, '').match(/\\[a-zA-Z]+/);
        if (bare) report.remaining.push(`${h}: 公式外裸露 LaTeX 指令 ${bare[0]}`);
        if (!katex) return;
        for (const s of segs) {
          if (s.t === 'text') continue;
          const warns = [];
          try { katex.renderToString(s.s, { displayMode: s.t === 'disp', throwOnError: true, strict: (c, mm) => { warns.push(mm); return 'ignore'; } }); }
          catch (e) { report.remaining.push(`${h}: KaTeX 报错 ${e.message.split('\n')[0].slice(0, 80)} ← ${s.s.trim().slice(0, 40)}`); continue; }
          warns.slice(0, 1).forEach(w => report.remaining.push(`${h}: KaTeX 警告 ${w.slice(0, 80)} ← ${s.s.trim().slice(0, 40)}`));
        }
      });
      const stray = src.split('\n').filter(l => l.includes('$$') && l.trim() !== '$$').length;
      if (stray) report.remaining.push(`有 ${stray} 行的 $$ 没有独占一行`);
      const by = {};
      (src.match(/^### \d+\.\d+\s*$/gm) || []).forEach(h => { const [c, q] = h.replace('### ', '').trim().split('.').map(Number); (by[c] = by[c] || []).push(q); });
      for (const [c, a] of Object.entries(by)) for (let i = 1; i < a.length; i++) if (a[i] !== a[i - 1] + 1) report.notes.push(`第${c}章题号不连续: ${a[i - 1]} → ${a[i]}`);
    }

    // ---- 主流程
    let result = text;
    if (opts.noise) {
      result = stripNoise(result);
      // 多余的“**…习题 2.53**”标题行、ASCII 框图代码块
      result = result.replace(/^\*\*[^*\n]*习题\s*\d+\.\d+\*\*[ \t]*\n+/gm, m => { report.removed.push('重复的题目标题行: ' + m.trim().slice(0, 40)); return ''; });
      result = result.replace(/^```[^\n]*\n([\s\S]*?)\n```[ \t]*\n+/gm, (m, b) => /-{2,}>|\+--/.test(b) ? (report.removed.push('ASCII 框图代码块: ' + b.split('\n')[0].slice(0, 40)), '') : m);
    }
    result = result.replace(/\\textit\{([^{}]*)\}/g, (m, x, off, s) => { if ((s.slice(0, off).match(/(?<!\\)\$/g) || []).length % 2) return m; bump('\\textit{} 改成 *斜体*'); return '*' + x + '*'; });
    result = fixBracketDelims(result);
    const { head, blocks } = splitBlocks(result);
    let bs = blocks.map(fixBare).map(fixLabels);
    if (opts.reorder) bs = bs.map(reorder);
    bs = bs.map(fixMath);
    result = (head + bs.join('')).replace(/\n{3,}/g, '\n\n').replace(/\n+$/, '\n');
    // 可选：纯数字标题 (### 1.1) 在 Pandoc 类解析器里 ID 都会变成 "section" 而报 Duplicate identifier，加显式唯一 ID
    if (opts.ids) result = result.replace(/^### (\d+)\.(\d+)[ \t]*$/gm, (m, c, q) => { bump('题号标题加唯一 ID {#q章-题}'); return `### ${c}.${q} {#q${c}-${q}}`; });
    validate(result);
    if (crlf) result = result.replace(/\n/g, '\r\n');
    return { result, report };
  }

  return { clean, tokenize };
});
