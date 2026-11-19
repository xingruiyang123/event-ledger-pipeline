# ============================================================
# 采集调度入口（模板）
#
# 用法：复制到数据根目录，改下面 4 个路径变量，然后执行
#   schtasks /Create /TN "EventLedger-Collect" ^
#     /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File <本文件路径>" ^
#     /SC MINUTE /MO 30 /F
#
# 三个必须知道的编码坑（都实测踩过）：
#  1. 文件必须存成 UTF-8 **with BOM**。PowerShell 5.1 靠 BOM 判断编码，
#     没有 BOM 就按系统 ANSI(GBK) 读，脚本里的中文路径全部乱码。
#  2. 变量名不能叫  —— PS 5.1 里  是自动变量，赋值会报
#     "Cannot overwrite variable Args"，脚本 exit=1。
#  3. 管道里的 Python 输出会被按 GBK 解码，中文变「淇℃簮涓昏〃」。
#     必须设 [Console]::OutputEncoding，只加 BOM 不够。
# ============================================================
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

"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 结束 exit=$rc ====" |
    Out-File -FilePath $log -Append -Encoding utf8

exit $rc
