# 竞品情报系统 —— 一键启动（看板 + 分析 UI）
#
# 用法（任选其一）：
#   右键 -> 使用 PowerShell 运行
#   或命令行：  powershell -ExecutionPolicy Bypass -File D:\Harness-spaces\start_all.ps1
#
# 参数：
#   -DashboardOnly   只启动看板（8000）
#   -AnalysisOnly    只启动分析 UI（8501）
#   -NoBrowser       启动但不打开浏览器
#   -Stop            停止两个服务
#
# 说明：
#   两个服务都是手动启动的进程，不是常驻服务；电脑重启后需重新运行本脚本。
#   每日采集任务由 Windows 计划任务负责（每天 09:00），与本脚本无关。
#
# 注意：本文件须保存为 UTF-8 **带 BOM**，否则 PowerShell 按 ANSI 读取会乱码。

param(
    [switch]$DashboardOnly,
    [switch]$AnalysisOnly,
    [switch]$NoBrowser,
    [switch]$Stop
)

$ErrorActionPreference = 'Continue'

$py       = 'C:\Users\EDY\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
$projMain = 'D:\Harness-spaces\爬取\intel-system'
$projCI   = 'D:\Harness-spaces\competitor-intel'

$env:PYTHONIOENCODING = 'utf-8'

function Test-Port([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# ── 停止服务 ────────────────────────────────────────────────
if ($Stop) {
    $killed = 0
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -like '*dashboard.py*' -or $_.CommandLine -like '*streamlit*'
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  已停止 PID $($_.ProcessId)"
        $killed++
    }
    if ($killed -eq 0) { Write-Host "  没有正在运行的服务" }
    exit 0
}

# ── 启动看板（8000）─────────────────────────────────────────
if (-not $AnalysisOnly) {
    if (Test-Port 8000) {
        Write-Host "[看板] 已在运行 (8000)"
    } else {
        Write-Host "[看板] 启动中 ..."
        Start-Process -FilePath $py `
            -ArgumentList 'scripts\dashboard.py', '--no-browser', '--port', '8000', '--host', '0.0.0.0' `
            -WorkingDirectory $projMain `
            -WindowStyle Hidden
        # 等待端口就绪（最多 15 秒）
        for ($i = 0; $i -lt 15; $i++) {
            Start-Sleep -Seconds 1
            if (Test-Port 8000) { break }
        }
        if (Test-Port 8000) {
            Write-Host "[看板] 就绪 -> http://127.0.0.1:8000"
        } else {
            Write-Host "[看板] 启动失败，请手动运行： python scripts\dashboard.py"
        }
    }
}

# ── 启动分析 UI（8501）──────────────────────────────────────
if (-not $DashboardOnly) {
    if (Test-Port 8501) {
        Write-Host "[分析] 已在运行 (8501)"
    } else {
        Write-Host "[分析] 启动中 ...（Streamlit 首次启动较慢，约 10~20 秒）"
        Start-Process -FilePath $py `
            -ArgumentList '-m', 'streamlit', 'run', 'app.py',
                          '--server.port', '8501', '--server.headless', 'true' `
            -WorkingDirectory $projCI `
            -WindowStyle Hidden
        for ($i = 0; $i -lt 25; $i++) {
            Start-Sleep -Seconds 1
            if (Test-Port 8501) { break }
        }
        if (Test-Port 8501) {
            Write-Host "[分析] 就绪 -> http://127.0.0.1:8501"
        } else {
            Write-Host "[分析] 启动失败，请手动运行： python -m streamlit run app.py"
        }
    }
}

# ── 打开浏览器 ──────────────────────────────────────────────
if (-not $NoBrowser) {
    if (Test-Port 8000) { Start-Process 'http://127.0.0.1:8000/' }
    if (-not $DashboardOnly -and (Test-Port 8501)) {
        Start-Process 'http://127.0.0.1:8501/'
    }
}

Write-Host ''
Write-Host '完成。停止服务请运行：'
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Stop"
