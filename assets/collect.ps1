# 事件台账采集调度 —— 计划任务入口（PowerShell 版）
#
# 为什么不用 .cmd：实测 cmd 的 %date% 在中文系统返回「周四 2026/11/19」，
# 用 %date:~5,2% 截取会得到「周四」而不是月份，导致 `logs\collect_周四` 这种
# 非法路径，整个脚本 exit 255。PowerShell 的 Get-Date -Format 没有这个问题。
#
# 为什么每 30 分钟调用一次，而不是给每个源建定时：
#   Windows 计划任务最小粒度是 1 分钟，但 693 个源各建一个任务不现实。
#   改成一个高频心跳 + 调度器内部按 safe_interval 判断到期，效果等价，
#   且新增源不用改任务配置。没到期的源直接跳过，不产生网络请求。

# 编码：Python 输出 UTF-8 字节，PowerShell 5.1 的管道默认按系统 ANSI(GBK)
# 解码，中文会变成「淇℃簮涓昏〃」这种乱码。这两行把控制台与管道编码钉成
# UTF-8。实测：只加 BOM 能让脚本自身的中文路径正确，但管道的 Python
# 输出仍可能乱码，所以两处都要设。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$ErrorActionPreference = "Continue"
$root = "D:\event-ledger-data"
$pipe = "D:\shared-skills\event-ledger-pipeline"
$py = "D:\anaconda\python.exe"
$reg = "C:\Users\21944\Desktop\七月恐怖事件\信源主表_可调度_20261119.xlsx"

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

$stamp = Get-Date -Format "yyyyMMdd"
$log = Join-Path $logDir "collect_$stamp.log"
$rep = Join-Path $logDir "report_$stamp.json"

"`n==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 开始 ====" |
    Out-File -FilePath $log -Append -Encoding utf8

# 注意：变量名不能叫 $args —— PowerShell 5.1 里 $args 是自动变量，赋值会报
# "Cannot overwrite variable Args because it is read-only or constant"，
# 导致脚本 exit=1。计划任务里用的是系统自带 powershell.exe（5.1），
# 这个坑必须避开，所以改用 $cliArgs。
$cliArgs = @(
    "-W", "ignore", (Join-Path $pipe "scripts\scheduler.py"),
    "--registry", $reg,
    "--store", $root,
    "--workers", "6",
    "--timeout", "25",
    "--report", $rep
)

& $py @cliArgs 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
$rc = $LASTEXITCODE
# ── 第二步：正文层 ──
# 每轮限量抓 300 篇，避免一次拖太久。首轮积压 1 万条约 35 轮消化完，
# 稳态后每轮只有几十条新增，几乎不耗时。
#
# 为什么与 feed 分两步：feed 是「这个源今天有什么」（分钟级），
# 正文是「把候选文章存下来」（小时级）。节奏不同，分开便于分别限流，
# 也便于单独看两层的成功率 —— 合并成一个数就判断不出问题在哪层。
"---- 正文层 ----" | Out-File -FilePath $log -Append -Encoding utf8
$artArgs = @(
    "-W", "ignore", (Join-Path $pipe "scripts\fetch_articles.py"),
    "--store", $root,
    "--workers", "6",
    "--timeout", "20",
    "--proxy", "http://127.0.0.1:17890",
    "--limit", "600"
)
& $py @artArgs 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
$rcArt = $LASTEXITCODE

if ($rc -eq 0) { $rc = $rcArt }

# ── 第三步：搜索补充 ──
# 只补「源有价值但被反爬挡住」的 P1 档（Times of Israel / MindaNews），
# 不做全量搜索 —— 元宝 0.014 元/次，全量数千条与收益不成比例。
# 用户 2026-11-20 的判断：「先把高价值源的搜索补充链路跑通，
# 之后再根据实际缺失情况决定是否值得投入（代理/出口 IP）。」
"---- 搜索补充 ----" | Out-File -FilePath $log -Append -Encoding utf8
$supArgs = @(
    "-W", "ignore", (Join-Path $pipe "scripts\supplement_by_search.py"),
    "--store", $root,
    "--search-cmd", 'D:\DSH\yb.cmd search "{q}" --cnt 10',
    "--tiers", "P1",
    "--sleep", "0.3"
)
& $py @supArgs 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
$rcSup = $LASTEXITCODE
# 搜索补充的返回值不参与总退出码。
# 它在「本轮没有需要补的条目」时返回 2，而那是正常状态不是故障 ——
# 传上去会让整条流水线每轮都显示失败，反而掩盖真正的失败。
if ($rcSup -ne 0) {
    "  搜索补充返回 $rcSup（无新增摘要，不影响流水线）" |
        Out-File -FilePath $log -Append -Encoding utf8
}

if (-not $rc) { $rc = 0 }
"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 结束 exit=$rc ====" |
    Out-File -FilePath $log -Append -Encoding utf8

exit $rc
