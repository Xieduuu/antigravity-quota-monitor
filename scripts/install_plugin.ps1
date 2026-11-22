<#
.SYNOPSIS
    一键将 Antigravity Quota & Context Monitor 安装到本机 Antigravity 全局插件目录。

.DESCRIPTION
    仅复制运行所需的轻量源码文件（plugin.json、mcp_config.json、hooks.json、assets、server、ui），
    不复制 .git、测试缓存或本地调试数据。安装后 Antigravity 会自动发现，无需重启主程序。
#>

param(
    [string]$TargetDir = (Join-Path $env:USERPROFILE ".gemini\config\plugins\antigravity-quota-monitor")
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "=== Antigravity Quota & Context Monitor 安装程序 ===" -ForegroundColor Cyan

# 检查 Python 可用性
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Warning "当前 PATH 中未检测到 python，请确保 Antigravity 运行环境可以调用 python。"
} else {
    Write-Host "检测到 Python: $($py.Source)" -ForegroundColor Green
}

Write-Host "目标插件目录: $TargetDir"

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir "assets") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir "server") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir "ui") | Out-Null

# 复制轻量核心文件
Copy-Item -Force (Join-Path $RepoRoot "plugin.json") $TargetDir
Copy-Item -Force (Join-Path $RepoRoot "mcp_config.json") $TargetDir
Copy-Item -Force (Join-Path $RepoRoot "hooks.json") $TargetDir
Copy-Item -Force (Join-Path $RepoRoot "README.md") $TargetDir
Copy-Item -Force (Join-Path $RepoRoot "assets\logo.svg") (Join-Path $TargetDir "assets")
Copy-Item -Force (Join-Path $RepoRoot "server\data_engine.py") (Join-Path $TargetDir "server")
Copy-Item -Force (Join-Path $RepoRoot "server\live_quota.py") (Join-Path $TargetDir "server")
Copy-Item -Force (Join-Path $RepoRoot "server\hook_tracker.py") (Join-Path $TargetDir "server")
Copy-Item -Force (Join-Path $RepoRoot "server\mcp_server.py") (Join-Path $TargetDir "server")
Copy-Item -Force (Join-Path $RepoRoot "ui\app.html") (Join-Path $TargetDir "ui")

Write-Host ""
Write-Host "安装完成！接下来在 Antigravity 2.0 桌面端中：" -ForegroundColor Green
Write-Host "  1. 打开右上角【右侧辅助栏】-> 点击 '+' -> 选择【额度与上下文】"
Write-Host "  2. 或在【左侧导航栏】点击【用量与额度】查看全局总览"
Write-Host "  3. 如此前已安装过旧版本，请在 设置 -> MCP Servers 中重启 quota-monitor 服务"
