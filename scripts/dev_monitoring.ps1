# 一键起本地监控栈：prometheus（抓取 9 个服务 + 求值告警规则）-> alertmanager（路由分发）。
# 用法：  powershell -File scripts/dev_monitoring.ps1
# 之后：  http://127.0.0.1:9090（Prometheus，看 alerts 页确认规则已加载）
#         http://127.0.0.1:9093（Alertmanager，确认收到告警）
#
# 前置：应用服务已在宿主运行（scripts/dev_services.py）；Docker Desktop 在运行。
# metrics_token 从 .env 的 METRICS_TOKEN 生成到 infra/monitoring/metrics_token（已 gitignore）——
# 没有它，抓取会全部 403（这是刻意的：指标端点不该无凭据暴露）。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# 1) 从 .env 提取 METRICS_TOKEN，生成 prometheus 的凭证文件
$env = Get-Content (Join-Path $root ".env") -Encoding UTF8
$token = ($env | Where-Object { $_ -match "^METRICS_TOKEN=(.+)$" }) -replace "^METRICS_TOKEN=", ""
if (-not $token) { throw ".env 里没有 METRICS_TOKEN；请先设置（见 .env.example）" }
[IO.File]::WriteAllText((Join-Path $root "infra/monitoring/metrics_token"), $token, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "OK  metrics_token 已生成"

# 2) 起 prometheus + alertmanager
Push-Location $root
docker compose --profile monitoring up -d
Pop-Location
Write-Host "OK  Prometheus  -> http://127.0.0.1:9090  （Status/Targets 应全部 up；Alerts 页应列出规则）"
Write-Host "OK  Alertmanager -> http://127.0.0.1:9093"
Write-Host "提示：alertmanager.yml 里的 webhook url 是占位符，接真实通知渠道时替换"