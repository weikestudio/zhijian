# 一键安装 qc-report-keeper (Windows PowerShell)
# 用法: 右键 -> 使用 PowerShell 运行, 或在 PowerShell 里:  .\install.ps1

$ErrorActionPreference = "Stop"
$REPO_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $REPO_DIR

Write-Host "==> qc-report-keeper 安装 (Windows)" -ForegroundColor Cyan

# 1. 检测 Python
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $pythonCmd) {
    Write-Host "❌ 未找到 Python，请先安装 Python 3.10+: https://www.python.org/downloads/" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
$PYVER = & $pythonCmd.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "✓ Python $PYVER ($($pythonCmd.Source))"

# 2. 装依赖
Write-Host "==> 安装 Python 依赖..."
& $pythonCmd.Source -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ pip install 失败" -ForegroundColor Red
    exit 1
}
Write-Host "✓ Python 依赖已安装"

# 3. 配置文件
if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "✓ 已生成 config.yaml（请填入真实 appid/secret）" -ForegroundColor Yellow
} else {
    Write-Host "✓ config.yaml 已存在，跳过"
}

# 4. 建归档目录（从 config.yaml 读）
Write-Host "==> 创建归档目录..."
& $pythonCmd.Source -c @"
import yaml, os
c = yaml.safe_load(open('config.yaml'))
for k in ['library_root', 'inbox_dir', 'pack_out_dir']:
    p = os.path.expanduser(c['storage'][k].replace('/', os.sep))
    os.makedirs(p, exist_ok=True)
    print(f'  ✓ {k}: {p}')
"@

# 5. 测试 OCR（RapidOCR 首次会自动下载模型）
Write-Host "==> 测试 OCR 引擎（首次会下载模型，约 15MB）..."
& $pythonCmd.Source -c @"
from rapidocr_onnxruntime import RapidOCR
ocr = RapidOCR()
print('  ✓ RapidOCR 引擎就绪')
"@
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ⚠ RapidOCR 初始化失败，首次运行时会自动重试" -ForegroundColor Yellow
}

# 6. 豆包 Skill 软链（Windows 用 junction）
$userSkills = "$env:APPDATA\DoubaoWork\Default\.doubaowork\agent_mode\workspace\.user_skills"
if (Test-Path $userSkills) {
    $linkPath = Join-Path $userSkills "qc-report-keeper"
    if (Test-Path $linkPath) {
        Remove-Item $linkPath -Force -Recurse
    }
    New-Item -ItemType Junction -Path $linkPath -Target $REPO_DIR | Out-Null
    Write-Host "✓ 已在 .user_skills/ 下链接 qc-report-keeper"
}

Write-Host ""
Write-Host "✅ 安装完成" -ForegroundColor Green
Write-Host "   启动工作台:  python bin\qc serve"
Write-Host "   环境自检:    python bin\qc doctor"
Write-Host "   配置文件:    $REPO_DIR\config.yaml"
Write-Host "   归档目录:    $libraryRoot"
Read-Host "按回车退出"
