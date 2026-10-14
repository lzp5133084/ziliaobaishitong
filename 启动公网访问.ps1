# 资料百事通 - 一键启动公网访问（Flask 本地服务 + cpolar 内网穿透）
# 作者：军哥懂保  微信：xunijiayuan
# 用法：右键“使用 PowerShell 运行”，或在 PowerShell 中执行 .\启动公网访问.ps1
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Test-Port($port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  资料百事通 · 公网访问一键启动" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan

# 1. 启动本地 Flask 服务（端口 8300）
if (Test-Port 8300) {
    Write-Host "[1/3] 本地服务已在 8300 端口运行" -ForegroundColor Green
} else {
    Write-Host "[1/3] 正在启动本地服务(python app.py)..." -ForegroundColor Yellow
    Start-Process -FilePath "python" -ArgumentList "app.py" -WorkingDirectory $root -WindowStyle Minimized
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Port 8300) { break }
    }
    if (Test-Port 8300) { Write-Host "      本地服务启动成功" -ForegroundColor Green }
    else { Write-Host "      本地服务启动失败，请先运行 python app.py 排查" -ForegroundColor Red; Read-Host "按回车退出"; exit 1 }
}

# 2. 启动 cpolar 隧道
$cpolar = Join-Path $root "tools\cpolar.exe"
if (-not (Test-Path $cpolar)) {
    Write-Host "[2/3] 未找到 tools\cpolar.exe，请先下载 cpolar 到该目录" -ForegroundColor Red
    Read-Host "按回车退出"; exit 1
}
$tunnelUrl = $null
try {
    $page = (Invoke-WebRequest -Uri "http://127.0.0.1:4040/http/in" -TimeoutSec 3 -UseBasicParsing).Content
    if ($page -match "https://[a-z0-9]+\.[a-z0-9.]*vip\.cpolar\.cn") { $tunnelUrl = $Matches[0] }
} catch {}

if ($tunnelUrl) {
    Write-Host "[2/3] cpolar 隧道已在运行" -ForegroundColor Green
} else {
    Write-Host "[2/3] 正在建立 cpolar 公网隧道..." -ForegroundColor Yellow
    Start-Process -FilePath $cpolar -ArgumentList "http","8300" -WorkingDirectory $root -WindowStyle Minimized
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        try {
            $page = (Invoke-WebRequest -Uri "http://127.0.0.1:4040/http/in" -TimeoutSec 3 -UseBasicParsing).Content
            if ($page -match "https://[a-z0-9]+\.[a-z0-9.]*vip\.cpolar\.cn") { $tunnelUrl = $Matches[0]; break }
        } catch {}
    }
}

# 3. 输出访问地址
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
if ($tunnelUrl) {
    Write-Host "  公网访问地址（发给任何人都能打开）：" -ForegroundColor Green
    Write-Host "  $tunnelUrl" -ForegroundColor White
    Write-Host ""
    Write-Host "  本机访问：http://127.0.0.1:8300" -ForegroundColor Gray
    Write-Host "  局域网访问：http://192.168.1.3:8300" -ForegroundColor Gray
    Write-Host "  隧道管理：http://127.0.0.1:4040" -ForegroundColor Gray
} else {
    Write-Host "  隧道暂未就绪，可能需要账号实名认证。" -ForegroundColor Red
    Write-Host "  请登录 https://dashboard.cpolar.com 完成实名认证后重试" -ForegroundColor Yellow
}
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "提示：免费版地址在重启 cpolar 后会变化，固定域名需升级套餐。" -ForegroundColor Yellow
Write-Host "本窗口关闭不影响服务运行。" -ForegroundColor Gray
Read-Host "按回车退出本窗口"
