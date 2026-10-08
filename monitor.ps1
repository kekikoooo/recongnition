# StudyHelp 构建监控面板（只读，不影响构建）
# 用法：powershell -ExecutionPolicy Bypass -File monitor.ps1 [-Port 8089] [-Refresh 3] [-Project 工作项名]
# 数据来源：/api/ingest/status、output/<工作项>/events.log（带时间的过程事件）、usage_log.jsonl、slots 文件数
param([int]$Port = 8089, [int]$Refresh = 3, [string]$Project = "", [switch]$Once)

$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Root = Join-Path $PSScriptRoot "output"
$StageName = @{ crop = "切题"; text = "整理题干"; solve = "解题"; verify = "检验"; aggregate = "汇编"; pdf = "编译PDF"; done = "完成"; error = "失败"; idle = "空闲" }

function Get-Status {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/ingest/status" -UseBasicParsing -TimeoutSec 4
        return ([Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json)
    } catch { return $null }
}

function Fmt-Span([double]$sec) {
    if ($sec -lt 0) { $sec = 0 }
    $t = [TimeSpan]::FromSeconds([math]::Round($sec))
    if ($t.TotalHours -ge 1) { return ("{0}时{1:00}分{2:00}秒" -f [int]$t.TotalHours, $t.Minutes, $t.Seconds) }
    return ("{0}分{1:00}秒" -f [int]$t.TotalMinutes, $t.Seconds)
}

function Bar([double]$pct, [int]$w = 30) {
    $n = [math]::Max(0, [math]::Min($w, [int]($pct / 100 * $w)))
    return ("[" + ("█" * $n) + ("░" * ($w - $n)) + "]")
}

function Pick-Project($st) {
    if ($Project) { return $Project }
    if ($st -and $st.project) { return $st.project }
    $d = Get-ChildItem $Root -Directory | Where-Object { Test-Path (Join-Path $_.FullName "events.log") } |
        Sort-Object { (Get-Item (Join-Path $_.FullName "events.log")).LastWriteTime } -Descending | Select-Object -First 1
    if ($d) { return $d.Name }
    return ""
}

function Parse-Events($file) {
    $ev = @()
    if (-not (Test-Path $file)) { return $ev }
    foreach ($ln in (Get-Content $file -Tail 6000 -Encoding UTF8)) {
        $p = $ln -split " \| ", 3
        if ($p.Count -lt 3) { continue }
        $dt = [datetime]::MinValue
        if (-not [datetime]::TryParse($p[0], [ref]$dt)) { continue }
        $ev += [pscustomobject]@{ T = $dt; Kind = $p[1]; Msg = $p[2] }
    }
    return $ev
}

while ($true) {
    $st = Get-Status
    $proj = Pick-Project $st
    $dir = if ($proj) { Join-Path $Root $proj } else { "" }
    $now = Get-Date
    $out = New-Object System.Collections.Generic.List[object]   # @(text, color)
    function L($t, $c = "Gray") { $out.Add(@($t, $c)) }

    L ("═" * 78) "DarkCyan"
    L ("  StudyHelp 构建监控    {0}    每 {1}s 刷新    Ctrl+C 退出" -f $now.ToString("yyyy-MM-dd HH:mm:ss"), $Refresh) "Cyan"
    L ("═" * 78) "DarkCyan"
    if (-not $st) { L "  连不上服务 http://127.0.0.1:$Port （服务没开？）" "Red" }
    if (-not $proj) { L "  还没有任何工作项" "Yellow"; }
    else {
        $stage = if ($st) { $st.stage } else { "?" }
        $sname = if ($StageName.ContainsKey("$stage")) { $StageName["$stage"] } else { "$stage" }
        $running = ($st -and $st.is_running)
        L ("  工作项   {0}" -f $proj) "White"
        $state = if ($running) { "运行中" } else { "未在运行" }
        $sc = if ($running) { "Green" } else { "Yellow" }
        L ("  状态     {0} · 阶段：{1}   {2}" -f $state, $sname, $(if ($st -and $st.message) { $st.message } else { "" })) $sc
        if ($st) { L ("  总进度   {0} {1}%" -f (Bar $st.pct), $st.pct) "Green" }
        if ($running -and $st.started) {
            $el = ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$st.started)
            L ("  已运行   {0}" -f (Fmt-Span $el)) "Gray"
        }
        if ($st -and $st.error) { L ("  错误     {0}" -f $st.error) "Red" }

        # —— 解题统计
        $total = 0
        foreach ($pj in (Get-ChildItem $dir -Recurse -Filter problems.json -Depth 1 | Where-Object { $_.FullName -notmatch "_未导入章节" })) {
            try { $total += @((Get-Content $pj.FullName -Raw -Encoding UTF8 | ConvertFrom-Json)).Count } catch {}
        }
        $solved = @(Get-ChildItem $dir -Recurse -Filter "slot_*.json" -Depth 2 | Where-Object { $_.FullName -notmatch "_未导入章节" }).Count
        $ev = Parse-Events (Join-Path $dir "events.log")
        $done5 = @($ev | Where-Object { $_.Kind -eq "题目" -and $_.Msg -match "完成" -and ($now - $_.T).TotalMinutes -le 5 }).Count
        $rate = $done5 / 5.0
        $left = [math]::Max(0, $total - $solved)
        $eta = if ($rate -gt 0 -and $left -gt 0) { Fmt-Span ($left / $rate * 60) } elseif ($left -eq 0 -and $total -gt 0) { "已全部完成" } else { "—" }
        L "" ; L "  ── 解题 ─────────────────────────────────────────────────────────────" "DarkGray"
        if ($total -gt 0) {
            L ("  已解     {0}/{1} 题   {2}" -f $solved, $total, (Bar (100.0 * $solved / $total))) "Green"
            L ("  速度     最近5分钟完成 {0} 题（{1:N1} 题/分钟）   预计剩余 {2}" -f $done5, $rate, $eta) "Gray"
        } else { L "  还没有切好的题目" "DarkGray" }

        # —— token
        $tok = 0; $tok5 = 0; $calls5 = 0
        $uf = Join-Path $dir "usage_log.jsonl"
        if (Test-Path $uf) {
            $nowEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            foreach ($ln in (Get-Content $uf -Encoding UTF8)) {
                try { $u = $ln | ConvertFrom-Json } catch { continue }
                $tok += [long]$u.total
                if (($nowEpoch - [double]$u.t) -le 300) { $tok5 += [long]$u.total; $calls5++ }
            }
        }
        L ("  token    累计 {0:N0}   最近5分钟 {1:N0}（{2} 次调用）" -f $tok, $tok5, $calls5) "Gray"

        # —— 正在进行的请求（发出了但还没返回）
        $inflight = @{}
        foreach ($e in $ev) {
            if ($e.Kind -eq "请求") { $inflight[($e.Msg -split " 已发出")[0]] = $e.T }
            elseif ($e.Kind -eq "返回" -or $e.Kind -eq "失败") { $inflight.Remove(($e.Msg -split " 用时")[0]) | Out-Null }
        }
        $infl = @($inflight.GetEnumerator() | Where-Object { ($now - $_.Value).TotalMinutes -le 30 } | Sort-Object Value)
        L "" ; L ("  ── 正在等待模型返回的请求：{0} 个 ────────────────────────────────" -f $infl.Count) "DarkGray"
        foreach ($i in ($infl | Select-Object -First 8)) {
            $w = ($now - $i.Value).TotalSeconds
            $c = if ($w -gt 180) { "Yellow" } else { "Gray" }
            L ("  {0}  已等 {1}   发出于 {2}" -f $i.Key, (Fmt-Span $w), $i.Value.ToString("HH:mm:ss")) $c
        }
        if ($infl.Count -gt 8) { L ("  … 还有 {0} 个" -f ($infl.Count - 8)) "DarkGray" }

        # —— 失败
        $fail10 = @($ev | Where-Object { $_.Kind -eq "失败" -and ($now - $_.T).TotalMinutes -le 10 }).Count
        $fc = if ($fail10 -gt 0) { "Red" } else { "DarkGray" }
        L "" ; L ("  最近10分钟失败/重试：{0} 次" -f $fail10) $fc

        # —— 阶段时间线
        L "" ; L "  ── 阶段时间线 ───────────────────────────────────────────────────────" "DarkGray"
        $stg = @($ev | Where-Object { $_.Kind -eq "阶段" })
        $firstRun = $null
        for ($k = [math]::Max(0, $stg.Count - 8); $k -lt $stg.Count; $k++) {
            $cur = $stg[$k]
            $next = if ($k + 1 -lt $stg.Count) { $stg[$k + 1].T } else { $now }
            L ("  {0}  {1}   （持续 {2}）" -f $cur.T.ToString("HH:mm:ss"), $cur.Msg, (Fmt-Span (($next - $cur.T).TotalSeconds))) "Yellow"
        }
        if ($stg.Count -eq 0) { L "  （还没有阶段记录：需要重启服务后新开始的构建才会写 events.log）" "DarkGray" }

        # —— 产物（网页顶部那一排按钮对应的文件：打印.md / 打印.pdf / Compact.md / All.md / 目录）
        L "" ; L "  ── 产物（每章：打印.md · 打印.pdf · Compact.md · All.md）──────────────" "DarkGray"
        function Mark($path) {
            if (Test-Path $path) { $i = Get-Item $path; return ("✓ {0:N0}KB {1}" -f ($i.Length / 1KB), $i.LastWriteTime.ToString("HH:mm")) }
            return "✗ 未生成"
        }
        foreach ($cd in (Get-ChildItem $dir -Directory -Filter "Chapter_*" | Sort-Object Name)) {
            $n = $cd.Name
            $parts = @(("打印.md " + (Mark (Join-Path $cd.FullName "${n}_Print.md"))),
                       ("打印.pdf " + (Mark (Join-Path $cd.FullName "${n}_Print.pdf"))),
                       ("Compact " + (Mark (Join-Path $cd.FullName "${n}_Compact_Solutions.md"))),
                       ("All " + (Mark (Join-Path $cd.FullName "${n}_All.md"))))
            $ok = (@($parts | Where-Object { $_ -match "✗" }).Count -eq 0)
            L ("  {0}  {1}" -f $n.Replace("Chapter_", "第"), ($parts -join "  │  ")) $(if ($ok) { "Green" } else { "Yellow" })
        }
        $bk = @(("Book_Print.md"), ("Book_Print.pdf"), ("Book_All.md"), ("Book_All.pdf"), ("Book_Timu_All.md"))
        $bs = ($bk | ForEach-Object { "{0} {1}" -f $_, $(if (Test-Path (Join-Path $dir $_)) { "✓" } else { "✗" }) }) -join "  "
        L ("  全书  {0}" -f $bs) "Cyan"
        L ("  目录  {0}" -f $dir) "DarkGray"

        # —— 最近事件
        L "" ; L "  ── 最近事件 ─────────────────────────────────────────────────────────" "DarkGray"
        foreach ($e in ($ev | Select-Object -Last 14)) {
            $c = switch ($e.Kind) { "失败" { "Red" } "返回" { "Green" } "请求" { "DarkCyan" } "阶段" { "Yellow" } "题目" { "White" } default { "Gray" } }
            $m = $e.Msg; if ($m.Length -gt 64) { $m = $m.Substring(0, 64) + "…" }
            L ("  {0}  {1}  {2}" -f $e.T.ToString("HH:mm:ss"), $e.Kind, $m) $c
        }
    }
    Clear-Host
    foreach ($o in $out) { Write-Host $o[0] -ForegroundColor $o[1] }
    if ($Once) { break }
    Start-Sleep -Seconds $Refresh
}
