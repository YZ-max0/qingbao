# Intel System - scheduled runner (Windows Task Scheduler)
#
# Portable launcher: resolves its own directory, so the project can be
# placed anywhere. Use this with Windows Task Scheduler for daily runs.
#
# Register a daily task (replace the path with your own):
#
#   $ps  = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
#   $dir = "D:\path\to\intel-system"
#   $act = New-ScheduledTaskAction -Execute $ps `
#          -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$dir\run_scheduled.ps1`""
#   $trg = New-ScheduledTaskTrigger -Daily -At 09:00
#   Register-ScheduledTask -TaskName 'IntelSystem_Daily' -Action $act -Trigger $trg
#
# NOTE: this file must be saved as UTF-8 **with BOM**. Without it,
# Windows PowerShell reads non-ASCII text as ANSI and mangles it.

$ErrorActionPreference = 'Continue'

# Project root = the directory containing this script
$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $proj 'data\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

$stamp = Get-Date -Format 'yyyyMMdd'
$log = Join-Path $logDir "run_$stamp.log"

# Append with explicit UTF-8. Do NOT use '*>>' here: on Windows PowerShell 5.1
# that writes UTF-16LE, which later reads as garbage.
function Write-Log([string]$msg) {
    $msg | Out-File -FilePath $log -Encoding utf8 -Append
}

Write-Log "============================================"
Write-Log "[START] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Log "============================================"

# ── 选择 Python 解释器 ─────────────────────────────────────
#
# 踩过的坑（2026-10-17 起连续三天任务失败）：
#   原来这里写死 'python'，依赖 PATH。计划任务环境下的 PATH 解析到了
#   另一个 Python（系统 Python 3.12），而依赖只装在别处，
#   于是 `import bs4` 失败、整条链路中断，且退出码 1 不易察觉。
#
# 现在按优先级探测，选中**能真正跑起来**的那个解释器：
#   1. PYTHON_EXE 环境变量（显式覆盖，最高优先级）
#   2. 项目内的 .venv（推荐做法：完全自包含）
#   3. 已安装本项目依赖（bs4）的解释器 —— 依次尝试已知候选
#   4. 兜底：PATH 里的 python
#
# 判定标准用 `import bs4`，因为它是本项目最基础的第三方依赖，
# 比检查 python 版本更能反映"这个解释器能不能跑起来"。

function Test-PythonUsable([string]$exe) {
    if (-not $exe) { return $false }
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $false }
    try {
        & $exe -c "import bs4" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$venvPy = Join-Path $proj '.venv\Scripts\python.exe'

$candidates = @()
if ($env:PYTHON_EXE) { $candidates += $env:PYTHON_EXE }
$candidates += $venvPy
# 本次部署实际使用的解释器（DSH 运行时）
$candidates += 'C:\Users\EDY\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
$candidates += 'python'

$py = $null
foreach ($c in $candidates) {
    if (Test-PythonUsable $c) {
        $py = $c
        break
    }
    if ($c) { Write-Log "  跳过不可用的解释器: $c" }
}

if (-not $py) {
    Write-Log "[FATAL] 找不到可用的 Python 解释器（需要能 import bs4）。"
    Write-Log "        请设置 PYTHON_EXE 环境变量，或在项目内创建 .venv 并安装 requirements.txt"
    exit 1
}

Write-Log "Python: $py"
$env:PYTHONIOENCODING = 'utf-8'

# ── Credentials ────────────────────────────────────────────
# The Python side reads config\llm.local.yaml by itself, so nothing must be
# exported here. We only report which source will be used, so a silently
# degraded run (rule-based summaries instead of LLM) shows up in the log.

# Alert webhook: set it here, or leave empty and configure config\alerts.yaml.
if (-not $env:ALERT_WEBHOOK_URL -and (Test-Path (Join-Path $proj 'config\secrets.local.ps1'))) {
    . (Join-Path $proj 'config\secrets.local.ps1')
}

$llmConf = Join-Path $proj 'config\llm.local.yaml'
if ($env:OPENAI_API_KEY) {
    Write-Log "LLM: using OPENAI_API_KEY from environment"
} elseif (Test-Path $llmConf) {
    $hasKey = Select-String -Path $llmConf -Pattern '^\s*api_key\s*:\s*["'']?sk-' -Quiet
    if ($hasKey) {
        Write-Log "LLM: using config\llm.local.yaml"
    } else {
        Write-Log "WARN: llm.local.yaml exists but has no api_key - LLM step will be skipped"
    }
} else {
    Write-Log "WARN: no LLM credentials found - LLM step will be skipped (rule-based fallback)"
}

Set-Location -LiteralPath $proj

& $py 'scripts\run_all.py' 2>&1 | Out-File -FilePath $log -Encoding utf8 -Append
$code = $LASTEXITCODE

# Optional companion steps (deep-analysis sync). Each is idempotent and
# skips itself when the companion project or credentials are absent.
& $py 'scripts\sync_reports.py' 2>&1 | Out-File -FilePath $log -Encoding utf8 -Append
& $py 'scripts\make_report_brief.py' 20 2>&1 | Out-File -FilePath $log -Encoding utf8 -Append
& $py 'scripts\refresh_sources.py' 3 2>&1 | Out-File -FilePath $log -Encoding utf8 -Append

Write-Log "[END] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exit=$code"

# Keep 30 days of logs
Get-ChildItem $logDir -Filter '*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
