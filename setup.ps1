$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw '需要安装 Python 3.12 和 Python Launcher (py)。'
}

& py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '创建 Python 3.12 虚拟环境失败。' }

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $python -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '安装 requirements.txt 依赖失败。' }

$envFile = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $PSScriptRoot '.env.example') $envFile
    $code = 'from pathlib import Path; import re, secrets; p=Path(".env"); s=p.read_text(encoding="utf-8"); p.write_text(re.sub(r"(?m)^WORKBENCH_SECRET=.*$", "WORKBENCH_SECRET=" + secrets.token_urlsafe(32), s), encoding="utf-8")'
    & $python -c $code
    if ($LASTEXITCODE -ne 0) { throw '生成本机 WORKBENCH_SECRET 失败。' }
}

Write-Host '安装完成。运行：.venv\Scripts\python.exe app.py serve'
